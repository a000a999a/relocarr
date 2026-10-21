from __future__ import annotations

from pathlib import Path
from typing import Literal

from .safety import resolve_within

LayoutMode = Literal["flat", "preserve"]


def compute_target_path(
    *,
    source_path: str | Path,
    staging_root: str | Path,
    target_root: str | Path,
    mode: LayoutMode,
) -> Path:
    """Map a file already imported into staging to its destination path.

    PLAN.md Section 7: the filename is always exactly what the *arr produced
    -- relocarr never builds folder names itself.

    - flat:     <target_root>/<filename>                  (Radarr default)
    - preserve: <target_root>/<path relative to staging_root>   (Sonarr, fixed)

    Raises PathSafetyError (via resolve_within) if the computed destination
    would fall outside target_root -- this also catches a source_path that
    isn't actually under staging_root.
    """
    source = Path(source_path)
    staging = Path(staging_root)

    relative = source.resolve(strict=False).relative_to(staging.resolve(strict=True))

    if mode == "flat":
        candidate = Path(target_root) / source.name
    elif mode == "preserve":
        candidate = Path(target_root) / relative
    else:
        raise ValueError(f"unknown layout mode: {mode!r}")

    return resolve_within(candidate, target_root)


def sidecar_paths(source_path: str | Path, extensions: list[str]) -> list[Path]:
    """Sibling files sharing the media file's stem with a configured
    extension (default: srt). PLAN.md Section 7.
    """
    source = Path(source_path)
    stem = source.stem
    found = []
    for sibling in source.parent.iterdir():
        if sibling == source:
            continue
        ext = sibling.suffix.lstrip(".")
        if sibling.stem == stem and ext in extensions:
            found.append(sibling)
    return sorted(found)
