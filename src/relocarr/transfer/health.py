from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HealthResult:
    ok: bool
    error: str | None = None


def probe_destination(target_root: str | Path, *, min_free_bytes: int = 100 * 1024 * 1024) -> HealthResult:
    """Section 12: health probe immediately before EACH copy attempt, doing
    real I/O rather than trusting reachability -- both Shield-wedge
    incidents this project hit (2026-09-19 and 2026-10-21) showed the mount
    answering ping/basic stat calls while actually dead for writes, so a
    stat-only check would have reported healthy wrongly in both cases.

    A small write+delete is the cheapest real test that can't lie the way a
    stat/list can on a mount that's up but the filesystem underneath is
    wedged.
    """
    root = Path(target_root)
    if not root.is_dir():
        return HealthResult(False, f"{root} does not exist or is not a directory")

    probe_file = root / f".relocarr_health_probe_{os.getpid()}"
    try:
        probe_file.write_bytes(b"relocarr health probe")
        probe_file.unlink()
    except OSError as exc:
        return HealthResult(False, f"write probe failed: {exc}")

    try:
        free = shutil.disk_usage(root).free
    except OSError as exc:
        return HealthResult(False, f"could not check free space: {exc}")
    if free < min_free_bytes:
        return HealthResult(False, f"only {free // (1024 * 1024)} MB free (minimum {min_free_bytes // (1024 * 1024)} MB)")

    return HealthResult(True)
