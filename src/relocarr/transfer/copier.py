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
#
# Confirmed live (building this module): exit code 23 is ALSO what rsync
# returns for a missing source -- a permanent error in our context (the
# job's source never reappears), not a transient destination issue, despite
# sharing the same exit code as genuine partial-transfer failures. The exact
# wording depends on WHAT's missing -- a missing file says
# `[sender] link_stat "..." failed: No such file or directory`, a missing
# parent directory says `[sender] change_dir "..." failed: ...` instead.
# `[sender]` itself is the reliable marker: it tags any error as being about
# OUR OWN local (source) path, as opposed to a receiver/destination-side
# problem, regardless of which specific syscall rsync was attempting.
_TRANSIENT_RSYNC_EXIT_CODES = {10, 11, 12, 23, 30}
_SENDER_SIDE_ERROR_MARKER = "[sender]"

# Root-caused live on the Shield, 2026-10-28: a sustained large-file write
# arrives faster than the destination can flush to its own storage, so a
# resource-constrained media box's write buffers can pile up in RAM until
# the OS's OOM killer kills the SMB server outright -- which drops the
# mount out from under the in-flight rsync process, wedging it in
# unkillable D-state (see rsync_copy's docstring) until the mount itself
# recovers. Throttling the write rate client-side keeps it within what the
# destination can actually drain, so the buffer never has a chance to grow
# unbounded. 10 MiB/s is comfortably above what even a slow USB thumbdrive
# sustains for writes, while leaving real headroom below whatever rate was
# triggering the OOM kill on the one real destination this was confirmed
# against -- never assumed to be the right number for every setup, which
# is why callers can override it per app (Settings -> App Config) rather
# than this being the last word.
_DEFAULT_BWLIMIT_MBPS = 10


@dataclass
class CopyResult:
    ok: bool
    error: str | None = None
    transient: bool = False


def rsync_copy(
    source: Path, dest: Path, *, timeout: float | None = None, bwlimit_mbps: int | None = None
) -> CopyResult:
    """Copy `source` to `dest` via rsync.

    No `--inplace`: rsync writes to a hidden temp file in the destination
    directory and renames atomically on success, which already gives us
    PLAN.md Section 12's ".part then atomic rename" behavior for free.

    `--partial-dir=.rsync-partial` (which implies `--partial`) keeps an
    interrupted transfer's in-progress bytes under a *stable* name --
    `.rsync-partial/<dest's own filename>`, relative to dest's own
    directory -- instead of plain `--partial`'s default of a fresh
    `.{name}.{random-6-chars}` temp file on every single invocation.
    Confirmed live, 2026-10-28: without --partial-dir, a job retried after
    an interrupted transfer never resumed its own prior attempt at all --
    each retry got a new random suffix and left the previous one behind
    as a permanent orphan, with nothing ever cleaning them up even once a
    later attempt succeeded (15 such orphans, ~22.9GB, accumulated on the
    real Shield before this was caught). With a stable partial-dir name, a
    retry finds and resumes its own prior attempt, and rsync itself
    removes the partial-dir entry on successful completion -- no manual
    cleanup needed on our side.

    Caveat confirmed the hard way during the 13.6 GB Shield wedge incident
    that motivated choosing rsync over a hand-rolled loop (PLAN.md Section
    12): if the destination mount itself wedges, the underlying rsync
    process can land in uninterruptible (D) kernel sleep -- not even
    SIGKILL can stop it then, so `timeout` here bounds *reporting*, not
    necessarily actual process termination. A caller must not assume a
    timed-out call freed anything.

    `--bwlimit` throttles the write rate to prevent the wedge above from
    happening in the first place -- see _DEFAULT_BWLIMIT_MBPS's comment
    for the root cause (an OOM-killed smbd) this addresses. `bwlimit_mbps`
    overrides the module default (Settings -> App Config, per-app -- not
    every destination has the same RAM/storage headroom the one this was
    first confirmed against does).
    """
    bwlimit = bwlimit_mbps if bwlimit_mbps is not None else _DEFAULT_BWLIMIT_MBPS
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "rsync",
                "-a",
                "--partial",
                "--partial-dir=.rsync-partial",
                f"--bwlimit={bwlimit}M",
                str(source),
                str(dest),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return CopyResult(ok=False, error=f"timed out after {exc.timeout}s", transient=True)

    if proc.returncode == 0:
        return CopyResult(ok=True)

    message = proc.stderr.strip() or proc.stdout.strip() or f"rsync exit {proc.returncode}"
    sender_side_error = _SENDER_SIDE_ERROR_MARKER in message
    transient = proc.returncode in _TRANSIENT_RSYNC_EXIT_CODES and not sender_side_error
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
