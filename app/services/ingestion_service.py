"""Ingestion lifecycle state machine.

request_upload: declared-size guard BEFORE token issuance; idempotent per
in-flight (seq_hash, source); token bound to the row by app-generated
token_ref. complete_upload is added in the verification phase.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AlreadyCached,
    IngestionNotFound,
    SourceNotEnabled,
    UploadTooLarge,
)
from app.core.hashing import normalize_sequence, sequence_hash
from app.schemas.ingestion import (
    DEFAULT_SOURCE,
    UploadCompleteResponse,
    UploadRequest,
    UploadRequestResponse,
)
from app.services.a3m_validator import A3mInvalid, A3mValidator
from app.services.classifier import PENDING_CLASSIFICATION, Classifier
from app.services.osdf_client import ObjectTooLarge, OsdfClient
from app.services.sequence_service import SequenceService
from app.services.settings_service import SettingsService
from app.services.token_issuer import TokenIssuer

log = logging.getLogger("af3cache.ingestion")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def source_slug(source: str) -> str:
    return _SLUG_RE.sub("-", source.lower()).strip("-")


@dataclass(frozen=True)
class _Resolved:
    seq_hash: str
    seq_length: int


@dataclass(frozen=True)
class PublishOverrides:
    """Caller-supplied metadata that overrides what the classifier / a3m would
    produce at publish (used by admin_ingest). None = fall back to computed."""

    species: str | None = None
    query_name: str | None = None
    n_sequences: int | None = None


class IngestionService:
    def __init__(
        self,
        session: AsyncSession,
        token_issuer: TokenIssuer,
        settings_service: SettingsService,
        osdf_client: OsdfClient,
        a3m_validator: A3mValidator,
        sequence_service: SequenceService,
        classifier: Classifier,
    ):
        self.session = session
        self.tokens = token_issuer
        self.settings_service = settings_service
        self.osdf = osdf_client
        self.a3m = a3m_validator
        self.sequences = sequence_service
        self.classifier = classifier

    @staticmethod
    def _resolve(req: UploadRequest) -> _Resolved:
        if req.sequence:
            norm = normalize_sequence(req.sequence)
            return _Resolved(sequence_hash(norm, already_normalized=True), len(norm))
        return _Resolved(req.seq_hash or "", req.seq_length or 0)

    async def request_upload(self, req: UploadRequest) -> UploadRequestResponse:
        eff = await self.settings_service.effective()

        # (1) refuse to even mint a write token for an oversize object
        if req.declared_size_bytes > eff.max_upload_size_bytes:
            raise UploadTooLarge(
                f"declared_size_bytes {req.declared_size_bytes} exceeds limit "
                f"{eff.max_upload_size_bytes}"
            )

        # (2) jobs may only contribute to the community source, and only while
        # that source is enabled (admin pauses community uploads by toggling
        # msa_sources.enabled). Curated sets are admin-ingested out-of-band.
        if req.source != DEFAULT_SOURCE:
            raise SourceNotEnabled(
                f"jobs may only contribute to {DEFAULT_SOURCE!r}"
            )
        if not await self.settings_service.source_enabled(req.source):
            raise SourceNotEnabled(f"source {req.source!r} is not enabled")

        r = self._resolve(req)
        subject = req.requester or "af3-cache-job"

        # (3) reject-unless-stale: a LIVE (active, non-idle) entry for this exact
        # (seq_hash, source, db_version) means "already cached" — tell the job to
        # use it instead of recomputing. A stale/idle entry is replaceable.
        live = (
            await self.session.execute(
                text(
                    """
                    select e.osdf_uri
                    from public.msa_cache_entries e
                    join public.msa_sequences sq on sq.id = e.sequence_id
                    join public.msa_sources s on s.name = e.source
                    where sq.seq_hash = :h and e.source = :src
                      and e.db_version = :dv and e.status = 'active'
                      and (s.idle_ttl_days is null
                           or e.last_accessed_at
                              > now() - make_interval(days => s.idle_ttl_days))
                    limit 1
                    """
                ),
                {"h": r.seq_hash, "src": req.source, "dv": req.db_version},
            )
        ).scalar_one_or_none()
        if live is not None:
            raise AlreadyCached(
                f"{req.source} a3m for this sequence (db_version="
                f"{req.db_version}) is already cached",
                extra={
                    "osdf_uri": live,
                    "source": req.source,
                    "db_version": req.db_version,
                },
            )

        existing = (
            await self.session.execute(
                text(
                    """
                    select id, osdf_uri from public.msa_ingestions
                    where seq_hash = :h and source = :s and db_version = :dv
                      and status in ('pending','uploaded')
                    """
                ),
                {"h": r.seq_hash, "s": req.source, "dv": req.db_version},
            )
        ).one_or_none()

        if existing is not None:
            # reuse the in-flight row's already-chosen store/object
            ingestion_id = str(existing.id)
            uri = existing.osdf_uri
        else:
            ingestion_id = str(uuid.uuid4())
            # load-balance: pick one store at random (independent geo-distributed
            # namespaces — no sharding/replication, just spread the writes)
            namespace = random.choice(
                self.settings_service.settings.pelican_store_list
            )
            uri = eff.osdf_namespace_template.format(
                namespace=namespace,
                seq_hash=r.seq_hash,
                source_slug=source_slug(req.source),
                ingestion_id=ingestion_id,
            )

        object_namespace = uri.rsplit("/", 1)[0]
        issued = await self.tokens.issue(
            subject=subject, object_uri=uri, ttl_seconds=eff.token_ttl_seconds
        )

        if existing is not None:
            # idempotent re-request: refresh the token on the existing row
            await self.session.execute(
                text(
                    """
                    update public.msa_ingestions
                       set token_ref = :ref, token_claim_jti = :jti,
                           token_expires_at = :exp, updated_at = now()
                     where id = :id
                    """
                ),
                {
                    "ref": issued.token_ref,
                    "jti": issued.claim_jti,
                    "exp": issued.expires_at,
                    "id": ingestion_id,
                },
            )
        else:
            try:
                async with self.session.begin_nested():
                    await self.session.execute(
                        text(
                            """
                            insert into public.msa_ingestions
                              (id, status, seq_hash, seq_length, molecule_type,
                               source, db_version, requested_sequence,
                               declared_size_bytes, declared_sha256, osdf_uri,
                               token_ref, token_claim_jti, token_expires_at,
                               requester)
                            values
                              (:id, 'pending', :h, :len, :mt, :src, :dv, :rseq,
                               :sz, :sha, :uri, :ref, :jti, :exp, :req)
                            """
                        ),
                        {
                            "id": ingestion_id,
                            "h": r.seq_hash,
                            "len": r.seq_length,
                            "mt": req.molecule_type,
                            "src": req.source,
                            "dv": req.db_version,
                            "rseq": (
                                normalize_sequence(req.sequence)
                                if req.sequence
                                else None
                            ),
                            "sz": req.declared_size_bytes,
                            "sha": req.declared_sha256,
                            "uri": uri,
                            "ref": issued.token_ref,
                            "jti": issued.claim_jti,
                            "exp": issued.expires_at,
                            "req": req.requester,
                        },
                    )
            except IntegrityError:
                # lost a concurrent race for the same (seq_hash, source,
                # db_version) in-flight: adopt the winner's row + object.
                won = (
                    await self.session.execute(
                        text(
                            """
                            select id, osdf_uri from public.msa_ingestions
                            where seq_hash = :h and source = :s
                              and db_version = :dv
                              and status in ('pending','uploaded')
                            """
                        ),
                        {"h": r.seq_hash, "s": req.source, "dv": req.db_version},
                    )
                ).one()
                ingestion_id = str(won.id)
                uri = won.osdf_uri
                issued = await self.tokens.issue(
                    subject=subject,
                    object_uri=uri,
                    ttl_seconds=eff.token_ttl_seconds,
                )
                object_namespace = uri.rsplit("/", 1)[0]
                await self.session.execute(
                    text(
                        """
                        update public.msa_ingestions
                           set token_ref = :ref, token_claim_jti = :jti,
                               token_expires_at = :exp, updated_at = now()
                         where id = :id
                        """
                    ),
                    {
                        "ref": issued.token_ref,
                        "jti": issued.claim_jti,
                        "exp": issued.expires_at,
                        "id": ingestion_id,
                    },
                )

        log.info(
            "upload-request ingestion=%s seq_hash=%s source=%s db_version=%s "
            "token_ref=%s",
            ingestion_id, r.seq_hash, req.source, req.db_version,
            issued.token_ref,
        )
        return UploadRequestResponse(
            ingestion_id=ingestion_id,
            osdf_uri=uri,
            osdf_namespace=object_namespace,
            write_token=issued.token,
            expires_at=issued.expires_at,
            ttl_seconds=eff.token_ttl_seconds,
            max_upload_size_bytes=eff.max_upload_size_bytes,
        )

    @staticmethod
    def _query_sequence(a3m_text: str) -> str:
        """The a3m's first record is the query; its plain residues = the
        sequence. Drop gaps ('-', '.'), uppercase (insertions are lowercase)."""
        first: list[str] = []
        seen_header = False
        for ln in a3m_text.splitlines():
            if ln.startswith(">"):
                if seen_header:
                    break
                seen_header = True
            elif seen_header and ln.strip():
                first.append(ln.strip())
        joined = "".join(first)
        return "".join(c for c in joined if c.isalpha()).upper()

    async def _reject(
        self, ingestion_id: str, reason: str, *, size: int | None = None,
        sha: str | None = None,
    ) -> UploadCompleteResponse:
        await self.session.execute(
            text(
                """
                update public.msa_ingestions
                   set status = 'rejected', reject_reason = :r,
                       observed_size_bytes = coalesce(:sz, observed_size_bytes),
                       observed_sha256 = coalesce(:sha, observed_sha256),
                       updated_at = now()
                 where id = :id
                """
            ),
            {"r": reason, "sz": size, "sha": sha, "id": ingestion_id},
        )
        log.warning("upload-complete ingestion=%s REJECTED reason=%s", ingestion_id, reason)
        return UploadCompleteResponse(
            ingestion_id=ingestion_id, status="rejected", reject_reason=reason
        )

    async def _resolve_classification(
        self, sequence: str, overrides: PublishOverrides | None
    ) -> tuple[str, str]:
        """species, query_name.

        Routes/callers split into two shapes:

        - Job upload (/v1/uploads/{id}/complete) passes `overrides=None`: we
          short-circuit to ('Pending Classification', 'Pending Classification')
          and DO NOT invoke the classifier. Diamond is off the critical path;
          an external BLAST pipeline backfills rows carrying this sentinel via
          POST /v1/admin/reclassify. (The /maintenance/reclassify sweep only
          picks up 'Unclassified' rows, not this sentinel.)
        - Admin ingest (/v1/admin/ingest) passes a PublishOverrides instance.
          If both species + query_name are provided the classifier is skipped;
          otherwise it runs to fill the gaps (unchanged behavior).
        """
        if overrides is None:
            return PENDING_CLASSIFICATION, PENDING_CLASSIFICATION
        if overrides.species is not None and overrides.query_name is not None:
            return overrides.species, overrides.query_name
        cls = await self.classifier.classify(sequence)
        return (
            overrides.species if overrides.species is not None else cls.species,
            overrides.query_name if overrides.query_name is not None else cls.query_name,
        )

    async def _simulate_complete(
        self, ingestion_id: str, row, overrides: PublishOverrides | None = None
    ) -> UploadCompleteResponse:
        """Simulation: don't fetch/verify — log the command we WOULD run and
        publish trusting the job's declared sha/size. Needs the requested
        sequence text (present when the request supplied `sequence`)."""
        log.info(
            "SIMULATE upload-complete ingestion=%s — would run: "
            "pelican object get %s -",
            ingestion_id, row.osdf_uri,
        )
        if not row.requested_sequence:
            log.warning(
                "SIMULATE ingestion=%s left 'uploaded': cannot publish without "
                "the sequence text (seq_hash-only request)",
                ingestion_id,
            )
            return UploadCompleteResponse(
                ingestion_id=ingestion_id,
                status="uploaded",
                reject_reason="simulated: awaiting OSDF (no sequence text)",
            )

        species, query_name = await self._resolve_classification(
            row.requested_sequence, overrides
        )
        n_seq = overrides.n_sequences if overrides else None

        seq_id = await self.sequences.get_or_create_sequence_id(
            row.requested_sequence, molecule_type=row.molecule_type
        )
        cache_entry_id = (
            await self.session.execute(
                text(
                    """
                    insert into public.msa_cache_entries
                      (sequence_id, source, db_version, osdf_uri,
                       a3m_size_bytes, a3m_sha256, n_sequences,
                       species, query_name,
                       status, last_accessed_at)
                    values (:sid, :src, :dv, :uri, :sz, :sha, :nseq,
                            :species, :qname,
                            'active', now())
                    on conflict (sequence_id, source, db_version) do update
                      set osdf_uri = excluded.osdf_uri,
                          a3m_size_bytes = excluded.a3m_size_bytes,
                          a3m_sha256 = excluded.a3m_sha256,
                          n_sequences = excluded.n_sequences,
                          species = excluded.species,
                          query_name = excluded.query_name,
                          status = 'active', stale_since = null,
                          last_accessed_at = now(), updated_at = now()
                    returning id
                    """
                ),
                {
                    "sid": seq_id,
                    "src": row.source,
                    "dv": row.db_version,
                    "uri": row.osdf_uri,
                    "sz": row.declared_size_bytes,
                    "sha": row.declared_sha256,
                    "nseq": n_seq,
                    "species": species,
                    "qname": query_name,
                },
            )
        ).scalar_one()
        await self.session.execute(
            text(
                """
                update public.msa_ingestions
                   set status = 'available',
                       observed_size_bytes = declared_size_bytes,
                       observed_sha256 = declared_sha256,
                       cache_entry_id = :ceid, reject_reason = null,
                       updated_at = now()
                 where id = :id
                """
            ),
            {"ceid": cache_entry_id, "id": ingestion_id},
        )
        log.warning(
            "SIMULATE ingestion=%s PUBLISHED unverified (declared sha/size "
            "trusted; real OSDF verification deferred) source=%s cache_entry=%s",
            ingestion_id, row.source, cache_entry_id,
        )
        return UploadCompleteResponse(
            ingestion_id=ingestion_id, status="available", osdf_uri=row.osdf_uri
        )

    async def complete_upload(
        self, ingestion_id: str, requester: str | None = None
    ) -> UploadCompleteResponse:
        row = (
            await self.session.execute(
                text(
                    """
                    select status, seq_hash, source, db_version, molecule_type,
                           osdf_uri, declared_sha256, declared_size_bytes,
                           requested_sequence, reject_reason
                    from public.msa_ingestions where id = :id
                    """
                ),
                {"id": ingestion_id},
            )
        ).one_or_none()
        if row is None:
            raise IngestionNotFound(f"no ingestion {ingestion_id}")

        # idempotency: callbacks are at-least-once; terminal states are no-ops
        if row.status in ("available", "rejected"):
            return UploadCompleteResponse(
                ingestion_id=ingestion_id,
                status=row.status,
                reject_reason=row.reject_reason,
                osdf_uri=row.osdf_uri if row.status == "available" else None,
            )

        await self.session.execute(
            text(
                "update public.msa_ingestions set status='uploaded', "
                "updated_at=now() where id=:id"
            ),
            {"id": ingestion_id},
        )

        return await self._verify_and_publish(ingestion_id, row)

    async def _verify_and_publish(
        self, ingestion_id: str, row,
        overrides: PublishOverrides | None = None,
        *, skip_verify: bool = False,
    ) -> UploadCompleteResponse:
        """Shared verification + publish path.

        Used by both `complete_upload` (job callback) and `admin_ingest` (bulk
        seed of pre-existing objects). Respects simulate mode (dryrun OSDF),
        otherwise fetches the object, verifies sha/size/format + that the a3m's
        query hashes to the declared seq_hash, then publishes a cache entry.
        `overrides` lets admin_ingest supply species/query_name/n_sequences
        directly (overriding classifier / a3m-derived values). `skip_verify`
        (admin-only) takes the trust-declared path without fetching even when the
        OSDF client is real — for the POST-before-upload bulk pipeline.
        """
        if self.osdf.is_simulated or skip_verify:
            return await self._simulate_complete(ingestion_id, row, overrides)

        eff = await self.settings_service.effective()

        # Release the DB connection across the slow `pelican object get`
        # subprocess (which can take tens of seconds with ?directread). The
        # session re-acquires from the pool on the next execute (post-fetch
        # writes), so the pool isn't starved by concurrent in-flight verifies.
        # NOTE: for admin_ingest this commits the pending ingestion-row INSERT;
        # if the fetch then raises OsdfUnavailable the row persists as
        # 'uploaded'. The bulk pipeline normally passes skip_verify=True and
        # doesn't take this path.
        await self.session.commit()

        # fetch (OsdfUnavailable propagates -> 503 retryable, row stays 'uploaded')
        try:
            obj = await self.osdf.fetch(
                row.osdf_uri, max_bytes=eff.max_upload_size_bytes
            )
        except ObjectTooLarge:
            return await self._reject(ingestion_id, "oversize")

        observed_sha = hashlib.sha256(obj.data).hexdigest()

        # ordered checks: size -> file hash -> a3m format -> semantic seq match
        if obj.size > eff.max_upload_size_bytes:
            return await self._reject(
                ingestion_id, "oversize", size=obj.size, sha=observed_sha
            )
        if observed_sha != row.declared_sha256:
            return await self._reject(
                ingestion_id, "sha256_mismatch", size=obj.size, sha=observed_sha
            )
        try:
            info = self.a3m.validate(obj.data)
        except A3mInvalid as e:
            return await self._reject(
                ingestion_id, e.reason, size=obj.size, sha=observed_sha
            )

        query_seq = self._query_sequence(obj.data.decode("utf-8"))
        if sequence_hash(query_seq) != row.seq_hash:
            return await self._reject(
                ingestion_id, "seq_hash_mismatch", size=obj.size, sha=observed_sha
            )

        # Metadata enrichment: caller overrides win; classifier fills gaps
        # (best-effort -> 'Unclassified'). n_sequences from override else a3m.
        species, query_name = await self._resolve_classification(
            query_seq, overrides
        )
        n_seq = (
            overrides.n_sequences
            if overrides and overrides.n_sequences is not None
            else info.n_sequences
        )

        # success: publish in one transaction
        seq_id = await self.sequences.get_or_create_sequence_id(
            query_seq, molecule_type=row.molecule_type
        )
        cache_entry_id = (
            await self.session.execute(
                text(
                    """
                    insert into public.msa_cache_entries
                      (sequence_id, source, db_version, osdf_uri,
                       a3m_size_bytes, a3m_sha256, n_sequences,
                       species, query_name,
                       status, last_accessed_at)
                    values (:sid, :src, :dv, :uri, :sz, :sha, :n,
                            :species, :qname,
                            'active', now())
                    on conflict (sequence_id, source, db_version) do update
                      set osdf_uri = excluded.osdf_uri,
                          a3m_size_bytes = excluded.a3m_size_bytes,
                          a3m_sha256 = excluded.a3m_sha256,
                          n_sequences = excluded.n_sequences,
                          species = excluded.species,
                          query_name = excluded.query_name,
                          status = 'active',
                          stale_since = null,
                          last_accessed_at = now(),
                          updated_at = now()
                    returning id
                    """
                ),
                {
                    "sid": seq_id,
                    "src": row.source,
                    "dv": row.db_version,
                    "uri": row.osdf_uri,
                    "sz": obj.size,
                    "sha": observed_sha,
                    "n": n_seq,
                    "species": species,
                    "qname": query_name,
                },
            )
        ).scalar_one()

        await self.session.execute(
            text(
                """
                update public.msa_ingestions
                   set status = 'available', observed_size_bytes = :sz,
                       observed_sha256 = :sha, observed_n_sequences = :n,
                       cache_entry_id = :ceid, reject_reason = null,
                       updated_at = now()
                 where id = :id
                """
            ),
            {
                "sz": obj.size,
                "sha": observed_sha,
                "n": info.n_sequences,
                "ceid": cache_entry_id,
                "id": ingestion_id,
            },
        )
        log.info(
            "verify+publish ingestion=%s AVAILABLE source=%s cache_entry=%s",
            ingestion_id, row.source, cache_entry_id,
        )
        return UploadCompleteResponse(
            ingestion_id=ingestion_id, status="available", osdf_uri=row.osdf_uri
        )

    def _mint_osdf_uri(self, template: str, ingestion_id: str, seq_hash: str,
                       source: str) -> str:
        """Render osdf_namespace_template for a new ingestion row — the same
        id-derived path the job upload path produces (see request_upload)."""
        stores = self.settings_service.settings.pelican_store_list
        return template.format(
            namespace=random.choice(stores) if stores else "",
            seq_hash=seq_hash,
            source_slug=source_slug(source),
            ingestion_id=ingestion_id,
        )

    async def admin_ingest(self, req) -> UploadCompleteResponse:
        """Admin-only: bulk-seed an ingestion for an a3m the caller has (or is
        about to put) on the origin. Bypasses the community-only upload gate
        (admin asserts the source). If req.osdf_uri is given it is used
        verbatim; otherwise the URI is minted from osdf_namespace_template
        against the new row's id — same as the job path — and returned to the
        caller so it knows where to upload. Runs the same verify+publish
        pipeline as a normal upload-complete.
        """
        # 1. source must exist and be enabled
        en = await self.settings_service.source_enabled(req.source)
        if en is None:
            raise SourceNotEnabled(f"unknown source: {req.source!r}")
        if not en:
            raise SourceNotEnabled(f"source {req.source!r} is not enabled")

        # 2. resolve seq_hash, seq_length, requested_sequence
        if req.sequence:
            norm = normalize_sequence(req.sequence)
            seq_hash = sequence_hash(norm, already_normalized=True)
            seq_length = len(norm)
            requested_sequence = norm
        else:
            seq_hash = req.seq_hash
            seq_length = req.seq_length
            requested_sequence = None

        # 3. reject-unless-stale (same rule as the job upload path)
        live = (
            await self.session.execute(
                text(
                    """
                    select e.osdf_uri
                    from public.msa_cache_entries e
                    join public.msa_sequences sq on sq.id = e.sequence_id
                    join public.msa_sources s on s.name = e.source
                    where sq.seq_hash = :h and e.source = :src
                      and e.db_version = :dv and e.status = 'active'
                      and (s.idle_ttl_days is null
                           or e.last_accessed_at
                              > now() - make_interval(days => s.idle_ttl_days))
                    limit 1
                    """
                ),
                {"h": seq_hash, "src": req.source, "dv": req.db_version},
            )
        ).scalar_one_or_none()
        if live is not None:
            raise AlreadyCached(
                f"{req.source} a3m for this sequence (db_version="
                f"{req.db_version}) is already cached",
                extra={"osdf_uri": live, "source": req.source,
                       "db_version": req.db_version},
            )

        # 4. insert ingestion row. Admin asserts the file exists (or will exist,
        #    with skip_verify) at osdf_uri; no real token is issued, so token_*
        #    are sentinel values (jti NULL, expires_at now()). When the caller
        #    omitted osdf_uri we mint it here, after the id — the row id IS the
        #    filename, same as the job upload path.
        ingestion_id = str(uuid.uuid4())
        if req.osdf_uri:
            osdf_uri = req.osdf_uri
        else:
            eff = await self.settings_service.effective()
            osdf_uri = self._mint_osdf_uri(
                eff.osdf_namespace_template, ingestion_id, seq_hash, req.source
            )
        try:
            async with self.session.begin_nested():
                await self.session.execute(
                    text(
                        """
                        insert into public.msa_ingestions
                          (id, status, seq_hash, seq_length, molecule_type,
                           source, db_version, requested_sequence,
                           declared_size_bytes, declared_sha256, osdf_uri,
                           token_ref, token_claim_jti, token_expires_at,
                           requester)
                        values
                          (:id, 'uploaded', :h, :len, :mt, :src, :dv, :rseq,
                           :sz, :sha, :uri,
                           :ref, null, now(), :req)
                        """
                    ),
                    {
                        "id": ingestion_id, "h": seq_hash, "len": seq_length,
                        "mt": req.molecule_type, "src": req.source,
                        "dv": req.db_version, "rseq": requested_sequence,
                        "sz": req.declared_size_bytes,
                        "sha": req.declared_sha256, "uri": osdf_uri,
                        "ref": uuid.uuid4().hex, "req": req.requester,
                    },
                )
        except IntegrityError as e:
            raise AlreadyCached(
                "another in-flight ingestion exists for "
                f"({req.source}, {req.db_version})",
                extra={"source": req.source, "db_version": req.db_version},
            ) from e

        row = (
            await self.session.execute(
                text(
                    """
                    select status, seq_hash, source, db_version, molecule_type,
                           osdf_uri, declared_sha256, declared_size_bytes,
                           requested_sequence, reject_reason
                    from public.msa_ingestions where id = :id
                    """
                ),
                {"id": ingestion_id},
            )
        ).one()
        log.info(
            "admin-ingest ingestion=%s source=%s db_version=%s uri=%s minted=%s",
            ingestion_id, req.source, req.db_version, osdf_uri,
            not req.osdf_uri,
        )
        overrides = PublishOverrides(
            species=req.species,
            query_name=req.query_name,
            n_sequences=req.n_sequences,
        )
        return await self._verify_and_publish(
            ingestion_id, row, overrides,
            skip_verify=bool(getattr(req, "skip_verify", False)),
        )
