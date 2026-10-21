from relocarr.arr.base import DownloadClientSettings, NotificationSettings, PreflightData, RootFolder
from relocarr.arr.preflight import evaluate_preflight


def _good_data(**overrides) -> PreflightData:
    defaults = dict(
        root_folders=[RootFolder(id=1, path="/staging/movies")],
        completed_download_handling_enabled=True,
        download_clients=[DownloadClientSettings(name="Transmission", remove_completed_downloads=False)],
        recycle_bin="",
        use_hardlinks=True,
        rename_enabled=True,
        import_extra_files=True,
        extra_file_extensions=["srt"],
        notifications=[NotificationSettings(name="Webhook", implementation="Webhook", any_trigger_enabled=True)],
        webhook_configured=True,
    )
    defaults.update(overrides)
    return PreflightData(**defaults)


def _status(results, name):
    return next(r for r in results if r.name == name).status


def test_all_pass_for_a_fully_correct_setup():
    results = evaluate_preflight(_good_data(), staging_root="/staging/movies", sidecar_extensions=["srt"])
    assert all(r.status == "pass" for r in results)


def test_fails_on_extra_root_folder():
    data = _good_data(root_folders=[RootFolder(1, "/staging/movies"), RootFolder(2, "/mnt/nvidia/movies")])
    results = evaluate_preflight(data, staging_root="/staging/movies", sidecar_extensions=[])
    assert _status(results, "Root folders") == "fail"


def test_fails_when_staging_root_not_a_root_folder_at_all():
    data = _good_data(root_folders=[RootFolder(1, "/mnt/nvidia/movies")])
    results = evaluate_preflight(data, staging_root="/staging/movies", sidecar_extensions=[])
    assert _status(results, "Root folders") == "fail"


def test_fails_when_completed_download_handling_disabled():
    data = _good_data(completed_download_handling_enabled=False)
    results = evaluate_preflight(data, staging_root="/staging/movies", sidecar_extensions=[])
    assert _status(results, "Completed Download Handling") == "fail"


def test_fails_when_a_download_client_has_remove_completed_on():
    data = _good_data(
        download_clients=[DownloadClientSettings(name="Transmission", remove_completed_downloads=True)]
    )
    results = evaluate_preflight(data, staging_root="/staging/movies", sidecar_extensions=[])
    assert _status(results, "Download client: Remove Completed Downloads") == "fail"


def test_warns_on_nonempty_recycle_bin():
    data = _good_data(recycle_bin="/mnt/recycle")
    results = evaluate_preflight(data, staging_root="/staging/movies", sidecar_extensions=[])
    assert _status(results, "Recycle Bin") == "warn"


def test_fails_when_hardlinks_off():
    data = _good_data(use_hardlinks=False)
    results = evaluate_preflight(data, staging_root="/staging/movies", sidecar_extensions=[])
    assert _status(results, "Use Hardlinks instead of Copy") == "fail"


def test_fails_when_rename_off():
    data = _good_data(rename_enabled=False)
    results = evaluate_preflight(data, staging_root="/staging/movies", sidecar_extensions=[])
    assert _status(results, "Rename") == "fail"


def test_warns_when_configured_sidecar_extension_not_imported_by_arr():
    data = _good_data(extra_file_extensions=["nfo"])  # missing srt
    results = evaluate_preflight(data, staging_root="/staging/movies", sidecar_extensions=["srt"])
    assert _status(results, "Import Extra Files") == "warn"


def test_warns_on_enabled_plex_notification():
    data = _good_data(
        notifications=[
            NotificationSettings(name="Webhook", implementation="Webhook", any_trigger_enabled=True),
            NotificationSettings(name="Plex Media Server", implementation="PlexServer", any_trigger_enabled=True),
        ]
    )
    results = evaluate_preflight(data, staging_root="/staging/movies", sidecar_extensions=[])
    assert _status(results, "Media server notifications") == "warn"


def test_passes_when_plex_notification_exists_but_all_triggers_disabled():
    data = _good_data(
        notifications=[
            NotificationSettings(name="Webhook", implementation="Webhook", any_trigger_enabled=True),
            NotificationSettings(name="Plex Media Server", implementation="PlexServer", any_trigger_enabled=False),
        ]
    )
    results = evaluate_preflight(data, staging_root="/staging/movies", sidecar_extensions=[])
    assert _status(results, "Media server notifications") == "pass"


def test_fails_when_no_webhook_configured():
    data = _good_data(webhook_configured=False)
    results = evaluate_preflight(data, staging_root="/staging/movies", sidecar_extensions=[])
    assert _status(results, "Webhook connection") == "fail"
