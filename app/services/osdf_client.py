"""OSDF object access behind an interface (stub-able for tests).

PelicanCliOsdfClient downloads `pelican object get <uri>?directread <tmpfile>`
to a private /tmp file, reads it back for verification, then deletes it.
`?directread` bypasses any cache so we hash the authoritative origin bytes
(and pelican's `-`/stdout sink yields nothing in some builds). Oversize ->
ObjectTooLarge (rejected); missing CLI / failed get -> OsdfUnavailable
(-> 503 retryable, no state corruption).
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass

from app.core.errors import OsdfUnavailable

log = logging.getLogger("af3cache.osdf")


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class ObjectTooLarge(Exception):
    """Fetched object exceeded the byte ceiling (caller rejects the ingestion)."""


@dataclass(frozen=True)
class FetchedObject:
    data: bytes
    size: int


class OsdfClient(abc.ABC):
    # When True, IngestionService.complete_upload skips the real fetch+verify
    # and publishes trusting the job's declared metadata (simulation mode).
    is_simulated: bool = False

    @abc.abstractmethod
    async def fetch(self, osdf_uri: str, *, max_bytes: int) -> FetchedObject: ...

    @abc.abstractmethod
    async def delete(self, osdf_uri: str) -> None:
        """Remove the object. Idempotent: a missing object is not an error."""


class StubOsdfClient(OsdfClient):
    """In-memory {uri: bytes}; drives the upload-complete verification tests."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects: dict[str, bytes] = objects or {}

    async def fetch(self, osdf_uri: str, *, max_bytes: int) -> FetchedObject:
        if osdf_uri not in self.objects:
            raise OsdfUnavailable(f"object not present in stub: {osdf_uri}")
        data = self.objects[osdf_uri]
        if len(data) > max_bytes:
            raise ObjectTooLarge(f"{len(data)} > {max_bytes}")
        return FetchedObject(data=data, size=len(data))

    async def delete(self, osdf_uri: str) -> None:
        self.objects.pop(osdf_uri, None)


class PelicanCliOsdfClient(OsdfClient):
    def __init__(
        self,
        binary: str = "pelican",
        *,
        concurrency: int = 4,
        queue_timeout: float = 60.0,
    ):
        self.binary = binary
        # Cap concurrent `pelican object get` subprocesses — directread is heavy
        # on the origin, and each verify holds CPU + /tmp space.
        self._sem = asyncio.Semaphore(concurrency)
        self._queue_timeout = queue_timeout

    async def fetch(self, osdf_uri: str, *, max_bytes: int) -> FetchedObject:
        # Wait for a slot. Time only the WAIT, not the fetch — a slow origin
        # shouldn't get killed mid-download once it's already running.
        try:
            async with asyncio.timeout(self._queue_timeout):
                await self._sem.acquire()
        except TimeoutError as e:
            raise OsdfUnavailable(
                "verify queue full (retryable; ingestion stays 'uploaded')"
            ) from e
        try:
            return await self._do_fetch(osdf_uri, max_bytes=max_bytes)
        finally:
            self._sem.release()

    async def _do_fetch(
        self, osdf_uri: str, *, max_bytes: int
    ) -> FetchedObject:
        # `?directread` -> read straight from the origin, bypassing caches, so we
        # verify the authoritative bytes. Download to a private /tmp file (the
        # `-`/stdout sink produces nothing in some pelican builds), read it back,
        # and always delete it. The osdf_uri stored in the DB stays unmodified.
        src = osdf_uri + ("&directread" if "?" in osdf_uri else "?directread")
        out_path = os.path.join("/tmp", f"osdf-verify-{uuid.uuid4().hex}.a3m")
        data = b""
        rc: int | str = "?"
        start = time.monotonic()
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.binary, "object", "get", src, out_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as e:
                raise OsdfUnavailable(
                    f"pelican CLI not found: {self.binary}"
                ) from e
            try:
                _, err = await proc.communicate()
            except asyncio.CancelledError:
                # Client disconnected mid-fetch. Don't leave a zombie pelican
                # downloading to /tmp; reap it, then propagate cancellation.
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                raise
            rc = proc.returncode
            if rc != 0:
                raise OsdfUnavailable(
                    f"pelican get exit {rc}: "
                    f"{err.decode(errors='replace').strip()[:200]}"
                )
            try:
                size = await asyncio.to_thread(os.path.getsize, out_path)
            except OSError as e:
                raise OsdfUnavailable(f"pelican get wrote no file: {e}") from e
            # pelican has already written the whole object to /tmp; the declared
            # -size check at upload-request is the primary size gate.
            if size > max_bytes:
                raise ObjectTooLarge(f"{size} > {max_bytes} bytes")
            data = await asyncio.to_thread(_read_bytes, out_path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(out_path)
            log.info(
                "osdf fetch %s took %.2fs rc=%s", osdf_uri,
                time.monotonic() - start, rc,
            )
        return FetchedObject(data=data, size=len(data))

    async def delete(self, osdf_uri: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "object", "rm", osdf_uri,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise OsdfUnavailable(f"pelican CLI not found: {self.binary}") from e
        rc = await proc.wait()
        if rc != 0:
            err = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
            # treat "not found" as success (idempotent); surface other failures
            if "not found" not in err.lower() and "no such" not in err.lower():
                raise OsdfUnavailable(f"pelican rm exit {rc}: {err.strip()[:200]}")


class DryRunOsdfClient(OsdfClient):
    """Simulation: never executes pelican. fetch/delete log the exact command
    they WOULD run; complete_upload sees is_simulated and publishes trusting
    the job's declared metadata (real verification deferred until OSDF ready)."""

    is_simulated = True

    async def fetch(self, osdf_uri: str, *, max_bytes: int) -> FetchedObject:
        # Not reached: complete_upload short-circuits on is_simulated. Logged +
        # raised so any accidental real use is loud, never silent.
        log.info(
            "SIMULATE fetch — would run: pelican object get %s?directread <tmp>",
            osdf_uri,
        )
        raise OsdfUnavailable("DryRunOsdfClient.fetch called (simulation mode)")

    async def delete(self, osdf_uri: str) -> None:
        log.info("SIMULATE delete — would run: pelican object rm %s", osdf_uri)


def build_osdf_client(
    impl: str,
    binary: str = "pelican",
    *,
    concurrency: int = 4,
    queue_timeout: float = 60.0,
) -> OsdfClient:
    impl = impl.lower()
    if impl == "stub":
        return StubOsdfClient()
    if impl == "dryrun":
        return DryRunOsdfClient()
    if impl == "pelican":
        return PelicanCliOsdfClient(
            binary, concurrency=concurrency, queue_timeout=queue_timeout
        )
    raise ValueError(f"unknown OSDF_CLIENT_IMPL: {impl!r}")
