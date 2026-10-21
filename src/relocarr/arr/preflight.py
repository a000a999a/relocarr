from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .base import PreflightData

Status = Literal["pass", "warn", "fail"]


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str


def evaluate_preflight(
    data: PreflightData, *, staging_root: str, sidecar_extensions: list[str]
) -> list[CheckResult]:
    """PLAN.md Section 10's prerequisite checklist, evaluated against real
    settings pulled from the *arr (arr/radarr.py, arr/sonarr.py's
    get_preflight_data). This is the generic subset that applies to both
    apps; the Sonarr-specific folder-name-collision check is v0.2 scope
    (PLAN.md Section 15's roadmap), not implemented here.
    """
    checks: list[CheckResult] = []

    extra_roots = [rf.path for rf in data.root_folders if rf.path.rstrip("/") != staging_root.rstrip("/")]
    if extra_roots:
        checks.append(
            CheckResult(
                "Root folders",
                "fail",
                f"Only the staging folder should be a root folder. Extra: {', '.join(extra_roots)}",
            )
        )
    elif not any(rf.path.rstrip("/") == staging_root.rstrip("/") for rf in data.root_folders):
        checks.append(CheckResult("Root folders", "fail", "Staging folder is not a root folder at all."))
    else:
        checks.append(CheckResult("Root folders", "pass", "Only the staging folder."))

    if data.completed_download_handling_enabled:
        checks.append(CheckResult("Completed Download Handling", "pass", "Enabled."))
    else:
        checks.append(
            CheckResult(
                "Completed Download Handling",
                "fail",
                "Disabled -- the *arr will never notice a finished download.",
            )
        )

    if not data.download_clients:
        checks.append(CheckResult("Download client: Remove Completed Downloads", "warn", "No download client configured in the *arr yet."))
    else:
        offenders = [c.name for c in data.download_clients if c.remove_completed_downloads]
        if offenders:
            checks.append(
                CheckResult(
                    "Download client: Remove Completed Downloads",
                    "fail",
                    f"Must be off (collides with relocarr's own cleanup): {', '.join(offenders)}",
                )
            )
        else:
            checks.append(CheckResult("Download client: Remove Completed Downloads", "pass", "Off."))

    if data.recycle_bin:
        checks.append(
            CheckResult("Recycle Bin", "warn", f"Set to {data.recycle_bin!r} -- should be empty, or deletes pile up there instead of freeing staging.")
        )
    else:
        checks.append(CheckResult("Recycle Bin", "pass", "Empty."))

    if data.use_hardlinks:
        checks.append(CheckResult("Use Hardlinks instead of Copy", "pass", "On."))
    else:
        checks.append(
            CheckResult("Use Hardlinks instead of Copy", "fail", "Off -- imports into staging will copy instead of hardlink, using extra space.")
        )

    if data.rename_enabled:
        checks.append(CheckResult("Rename", "pass", "On."))
    else:
        checks.append(CheckResult("Rename", "fail", "Off -- the *arr's naming format is relocarr's only source of the final filename."))

    missing_sidecars = [ext for ext in sidecar_extensions if ext not in data.extra_file_extensions]
    if data.import_extra_files and not missing_sidecars:
        checks.append(CheckResult("Import Extra Files", "pass", f"Includes {', '.join(sidecar_extensions) or '(none configured)'}."))
    elif not data.import_extra_files:
        checks.append(CheckResult("Import Extra Files", "warn", "Off -- sidecar files (subtitles, etc.) won't be imported at all."))
    else:
        checks.append(
            CheckResult(
                "Import Extra Files",
                "warn",
                f"relocarr is configured to copy {', '.join(missing_sidecars)} sidecars, but the *arr won't import them.",
            )
        )

    noisy_notifications = [n.name for n in data.notifications if n.implementation != "Webhook" and n.any_trigger_enabled]
    if noisy_notifications:
        checks.append(
            CheckResult(
                "Media server notifications",
                "warn",
                f"These will try to scan the staging path, which they can't see: {', '.join(noisy_notifications)}",
            )
        )
    else:
        checks.append(CheckResult("Media server notifications", "pass", "None enabled (or none configured)."))

    if data.webhook_configured:
        checks.append(CheckResult("Webhook connection", "pass", "Configured."))
    else:
        checks.append(CheckResult("Webhook connection", "fail", "Not configured -- relocarr will never receive import events."))

    return checks
