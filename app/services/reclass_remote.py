"""ssh/scp client for the NR-BLAST box: upload a FASTA, `reclass-submit`,
`reclass-status`.

Same subprocess discipline as the mmseqs runner: argv arrays (never a shell
string on OUR side), bounded by a timeout, killed on timeout/cancel, stderr
truncated in logs. The remote command string is built only from values we
control (config paths, our own timestamped file name, a validated job id), so
nothing user-supplied reaches the remote shell.

Key handling is deliberately explicit: `-i <key> -o IdentitiesOnly=yes` when a
key file is configured, and a pinned known_hosts with StrictHostKeyChecking=yes
when one is configured (accept-new otherwise, which is what a fresh container
needs on its first connection).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import re
import shlex
import time
from pathlib import Path

log = logging.getLogger("af3cache.reclass.remote")

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TAIL = 200


class RemoteReclassError(Exception):
    """A remote step failed. `reason` is a closed set: binary_missing | timeout
    | nonzero_exit | bad_job_id | parse_error."""

    def __init__(
        self, step: str, *, reason: str, returncode: int | None = None, stderr_tail: str = ""
    ):
        self.step = step
        self.reason = reason
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        detail = f"reclass {step}: {reason}"
        if returncode is not None:
            detail += f" (exit {returncode})"
        if stderr_tail:
            detail += f": {stderr_tail}"
        super().__init__(detail)


@dataclasses.dataclass(frozen=True)
class RemoteReclass:
    remote: str  # user@host
    data_dir: str  # e.g. /media/volume/mmseq2-data (FASTA goes to <data_dir>/inbox/)
    submit_cmd: str  # absolute path of reclass-submit on the box
    status_cmd: str  # absolute path of reclass-status on the box
    ssh_binary: str = "ssh"
    scp_binary: str = "scp"
    key_file: str | None = None
    known_hosts_file: str | None = None
    ssh_timeout: float = 120.0
    scp_timeout: float = 600.0

    def ssh_options(self) -> list[str]:
        opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
        if self.key_file:
            opts += ["-i", self.key_file, "-o", "IdentitiesOnly=yes"]
        if self.known_hosts_file:
            opts += [
                "-o", f"UserKnownHostsFile={self.known_hosts_file}",
                "-o", "StrictHostKeyChecking=yes",
            ]
        else:
            opts += ["-o", "StrictHostKeyChecking=accept-new"]
        return opts

    # --- operations ----------------------------------------------------------

    async def upload(self, local: Path, name: str) -> None:
        dest = f"{self.remote}:{self.data_dir}/inbox/{name}"
        await self._run(
            "scp", [self.scp_binary, "-q", *self.ssh_options(), str(local), dest], self.scp_timeout
        )

    async def submit(self, name: str) -> str:
        """Run `reclass-submit inbox/<name>`; return the job id it prints."""
        remote_cmd = f"{shlex.quote(self.submit_cmd)} {shlex.quote('inbox/' + name)}"
        _, out, err = await self._run(
            "submit", [self.ssh_binary, *self.ssh_options(), self.remote, remote_cmd],
            self.ssh_timeout,
        )
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        job_id = lines[-1] if lines else ""
        if not _JOB_ID_RE.match(job_id):
            raise RemoteReclassError(
                "submit", reason="bad_job_id", stderr_tail=(job_id or err.strip())[-_TAIL:]
            )
        return job_id

    async def status(self, job_id: str) -> dict:
        """Run `reclass-status <job>`; return the parsed JSON. UNKNOWN comes
        back with exit 2 and is still valid JSON, so 2 is accepted."""
        if not _JOB_ID_RE.match(job_id):
            raise RemoteReclassError("status", reason="bad_job_id")
        remote_cmd = f"{shlex.quote(self.status_cmd)} {shlex.quote(job_id)}"
        _, out, err = await self._run(
            "status", [self.ssh_binary, *self.ssh_options(), self.remote, remote_cmd],
            self.ssh_timeout, ok_codes=(0, 2),
        )
        try:
            payload = json.loads(out)
        except ValueError as e:
            raise RemoteReclassError(
                "status", reason="parse_error", stderr_tail=(out.strip() or err.strip())[-_TAIL:]
            ) from e
        if not isinstance(payload, dict):
            raise RemoteReclassError("status", reason="parse_error", stderr_tail="not an object")
        return payload

    # --- subprocess ----------------------------------------------------------

    async def _run(
        self, step: str, argv: list[str], max_seconds: float, ok_codes: tuple[int, ...] = (0,)
    ) -> tuple[int, str, str]:
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            log.warning("reclass %s: binary not found: %s", step, argv[0])
            raise RemoteReclassError(step, reason="binary_missing") from e
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=max_seconds)
        except TimeoutError as e:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            log.warning("reclass %s: timed out after %.0fs", step, max_seconds)
            raise RemoteReclassError(step, reason="timeout") from e
        except asyncio.CancelledError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        out = out_b.decode(errors="replace")
        err = err_b.decode(errors="replace")
        elapsed = time.monotonic() - t0
        rc = proc.returncode or 0
        if rc not in ok_codes:
            tail = err.strip()[-_TAIL:] or out.strip()[-_TAIL:]
            log.warning("reclass %s: exit %d in %.2fs: %s", step, rc, elapsed, tail)
            raise RemoteReclassError(step, reason="nonzero_exit", returncode=rc, stderr_tail=tail)
        log.info("reclass %s: ok in %.2fs", step, elapsed)
        return rc, out, err
