from __future__ import annotations

from pathlib import Path


class PathSafetyError(ValueError):
    pass


def resolve_within(path: str | Path, root: str | Path, *, must_exist: bool = True) -> Path:
    """Resolve `path` and verify it is inside `root` (symlinks included).

    Used before every delete and every write target, per PLAN.md Section 12:
    "realpath checks on every delete; refuse anything outside the staging
    root; never follow symlinks out of it."

    Raises PathSafetyError if `path` resolves outside `root`. If `root`
    itself does not exist, raises FileNotFoundError when must_exist=True
    (the default -- appropriate for the staging root, which is always a
    local path and must exist), or resolves it lexically without requiring
    existence when must_exist=False (for a destination root, which can
    legitimately be offline/unmounted -- confirmed in production,
    2026-10-22: the first real webhook against the live deployment 500'd
    here because the Shield was off, before any job even got created. A
    destination being down is an ordinary, expected condition this whole
    project exists to handle -- see Section 12 -- not a reason to fail
    before a job exists to retry/back off on).
    """
    resolved_root = Path(root).resolve(strict=must_exist)
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
