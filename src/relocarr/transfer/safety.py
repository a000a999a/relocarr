from __future__ import annotations

from pathlib import Path


class PathSafetyError(ValueError):
    pass


def resolve_within(path: str | Path, root: str | Path) -> Path:
    """Resolve `path` and verify it is inside `root` (symlinks included).

    Used before every delete and every write target, per PLAN.md Section 12:
    "realpath checks on every delete; refuse anything outside the staging
    root; never follow symlinks out of it."

    Raises PathSafetyError if `path` resolves outside `root`, or if `root`
    itself does not exist.
    """
    resolved_root = Path(root).resolve(strict=True)
    resolved_path = Path(path).resolve(strict=False)

    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise PathSafetyError(
            f"{path!r} resolves to {resolved_path} which is outside root {resolved_root}"
        ) from exc

    return resolved_path


def assert_distinct_roots(staging_root: str | Path, target_root: str | Path) -> None:
    """PLAN.md Section 10: staging must not be inside the destination or vice
    versa. relocarr refuses to start otherwise.
    """
    staging = Path(staging_root).resolve(strict=True)
    target = Path(target_root).resolve(strict=True)

    if staging == target:
        raise PathSafetyError("staging root and target root must not be the same path")
    if staging in target.parents or target in staging.parents:
        raise PathSafetyError(
            f"staging root {staging} and target root {target} must not be nested"
        )
