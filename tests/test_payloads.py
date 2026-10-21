import json
from pathlib import Path

from relocarr.arr.payloads import parse_radarr_webhook, parse_sonarr_webhook

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_parses_real_captured_download_event_without_download_id():
    # tests/fixtures/radarr_webhook_download_no_downloadid.json is the ACTUAL
    # payload captured live against Radarr 6.4.4 during spike 2 (2026-10-21,
    # a manual import) -- not synthetic; only titles, IDs and paths anonymized.
    payload = _load("radarr_webhook_download_no_downloadid.json")

    files = parse_radarr_webhook(payload)

    assert len(files) == 1
    f = files[0]
    assert f.unmonitor_id == 21
    assert f.file_id == "19"
    assert f.source_path.endswith("Example Film.2002.mp4")
    assert f.size_bytes == 2038218264
    assert f.download_id is None  # the actual finding this spike produced
    assert f.is_upgrade is False


def test_parses_download_event_with_download_id():
    # Real structure, augmented with downloadId/downloadClient -- fields
    # confirmed present on a genuine download-triggered import via Radarr's
    # History API the same session, not literally captured as a fired
    # webhook (Sonarr has zero series; the three real imports that evening
    # predated the webhook being wired up). See arr/payloads.py docstring.
    payload = _load("radarr_webhook_download_with_downloadid.json")

    files = parse_radarr_webhook(payload)

    assert len(files) == 1
    assert files[0].download_id == "0123456789ABCDEF0123456789ABCDEF01234567"


def test_ignores_non_download_event_types():
    payload = {"eventType": "Test", "movie": {"id": 1}}
    assert parse_radarr_webhook(payload) == []


def test_ignores_download_event_missing_movie_file():
    # e.g. a Grab-adjacent payload shape that doesn't actually carry a file yet
    payload = {"eventType": "Download", "movie": {"id": 1}}
    assert parse_radarr_webhook(payload) == []


def test_sonarr_single_episode_file():
    payload = {
        "eventType": "Download",
        "series": {"id": 5},
        "episodes": [{"id": 100}],
        "episodeFile": {"id": 200, "path": "/staging/tv/Show/Season 1/ep1.mkv", "size": 123},
        "downloadId": "HASH1",
    }
    files = parse_sonarr_webhook(payload)
    assert len(files) == 1
    assert files[0].unmonitor_id == 100  # episode id, not series id (Section 6)
    assert files[0].file_id == "200"


def test_sonarr_multi_episode_file_uses_first_episode_for_unmonitor():
    payload = {
        "eventType": "Download",
        "series": {"id": 5},
        "episodes": [{"id": 100}, {"id": 101}],
        "episodeFiles": [{"id": 200, "path": "/staging/tv/Show/multi.mkv", "size": 456}],
        "downloadId": "HASH2",
    }
    files = parse_sonarr_webhook(payload)
    assert len(files) == 1
    assert files[0].unmonitor_id == 100
