from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

# rsync exit codes treated as transient/retryable, per `man rsync`:
#   10 = error in socket I/O       12 = error in rsync protocol data stream
#   11 = error in file I/O         23 = partial transfer due to error
#   30 = timeout in data send/receive
# This is a starting guess, not yet validated against a real failure (spike 9
# only reproduced a success-path rsync; the actual wedge in PLAN.md Section 16
# happened with `cp`, before this module existed). Revisit once rsync itself
# has failed against a real degraded destination.
_TRANSIENT_RSYNC_EXIT_CODES = {10, 11, 12, 23, 30}


@dataclass
class CopyResult:
    ok: bool
    error: str | None = None
    transient: bool = False


def rsync_copy(source: Path, dest: Path, *, timeout: float | None = None) -> CopyResult:
    """Copy `source` to `dest` via rsync.

    No `--inplace`: rsync writes to a hidden temp file in the destination
    directory and renames atomically on success, which already gives us
    PLAN.md Section 12's ".part then atomic rename" behavior for free.

    `--partial` keeps that temp file on failure/interruption instead of
    deleting it, so a retry resumes rather than restarting -- the whole
    reason rsync was chosen over a hand-rolled loop (see PLAN.md Section 12,
    added after the 13.6 GB Shield wedge incident).

    Caveat confirmed the hard way during that incident: if the destination
    mount itself wedges, the underlying `cp`/rsync process can land in
    uninterruptible (D) kernel sleep -- not even SIGKILL can stop it then,
    so `timeout` here bounds *reporting*, not necessarily actual process
    termination. A caller must not assume a timed-out call freed anything.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["rsync", "-a", "--partial", str(source), str(dest)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return CopyResult(ok=False, error=f"timed out after {exc.timeout}s", transient=True)

    if proc.returncode == 0:
        return CopyResult(ok=True)

    transient = proc.returncode in _TRANSIENT_RSYNC_EXIT_CODES
    message = proc.stderr.strip() or proc.stdout.strip() or f"rsync exit {proc.returncode}"
    return CopyResult(ok=False, error=message, transient=transient)


def verify(source: Path, dest: Path, mode: str) -> CopyResult:
    """size (default) or checksum verification, per PLAN.md Section 12."""
    if not dest.exists():
        return CopyResult(ok=False, error="destination file missing after copy")

    if mode == "size":
        if source.stat().st_size != dest.stat().st_size:
            return CopyResult(ok=False, error="size mismatch")
        return CopyResult(ok=True)

    if mode == "checksum":
        if _sha256(source) != _sha256(dest):
            return CopyResult(ok=False, error="checksum mismatch")
        return CopyResult(ok=True)

    raise ValueError(f"unknown verify mode: {mode!r}")


def _sha256(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
