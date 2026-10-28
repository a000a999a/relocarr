import shutil
from unittest.mock import patch

import pytest

from relocarr.transfer.copier import rsync_copy, verify

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


def test_rsync_copy_uses_a_stable_partial_dir_not_rsyncs_random_default(tmp_path):
    # Confirmed live, 2026-10-28: without --partial-dir, every retry of an
    # interrupted transfer got a fresh random-suffixed temp file instead of
    # resuming its own prior attempt, leaving the old one orphaned forever
    # (15 such files, ~22.9GB, found accumulated on the real Shield). A
    # stable partial-dir name lets a retry find and resume its own attempt.
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rsync_copy(tmp_path / "src" / "movie.mkv", tmp_path / "dest" / "movie.mkv")

    args = mock_run.call_args[0][0]
    assert "--partial-dir=.rsync-partial" in args


def test_rsync_copy_throttles_bandwidth_to_avoid_oom_killing_the_shields_smbd(tmp_path):
    # Root-caused live, 2026-10-28: a sustained large-file write outpaced the
    # Shield's own storage flush rate, piling up smbd's write buffers in RAM
    # until Android's OOM killer killed smbd outright, dropping the mount
    # out from under an in-flight rsync and wedging it in unkillable D-state.
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rsync_copy(tmp_path / "src" / "movie.mkv", tmp_path / "dest" / "movie.mkv")

    args = mock_run.call_args[0][0]
    assert "--bwlimit=10M" in args


def test_rsync_copy_consumes_a_pre_existing_partial_dir_entry_on_success(tmp_path):
    # Simulates a retry picking up after an earlier interrupted attempt --
    # rsync itself should find the partial-dir entry, use it, and remove it
    # once the transfer completes; nothing on our side has to clean it up.
    source = tmp_path / "source.bin"
    source.write_bytes(b"relocarr" * 100_000)
    dest = tmp_path / "dest" / "source.bin"
    partial_dir = dest.parent / ".rsync-partial"
    partial_dir.mkdir(parents=True)
    (partial_dir / "source.bin").write_bytes(source.read_bytes()[:400_000])

    result = rsync_copy(source, dest)

    assert result.ok, result.error
    assert dest.read_bytes() == source.read_bytes()
    assert not partial_dir.exists()


def test_rsync_copy_and_verify_size(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * 1024)
    dest = tmp_path / "dest" / "source.bin"

    result = rsync_copy(source, dest)
    assert result.ok, result.error

    check = verify(source, dest, mode="size")
    assert check.ok, check.error


def test_rsync_copy_and_verify_checksum(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"relocarr" * 10000)
    dest = tmp_path / "dest" / "source.bin"

    assert rsync_copy(source, dest).ok
    assert verify(source, dest, mode="checksum").ok


def test_verify_detects_size_mismatch(tmp_path):
    source = tmp_path / "source.bin"
    dest = tmp_path / "dest.bin"
    source.write_bytes(b"a" * 100)
    dest.write_bytes(b"a" * 50)

    result = verify(source, dest, mode="size")
    assert not result.ok
    assert "size mismatch" in result.error


def test_verify_detects_missing_destination(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a")
    dest = tmp_path / "does-not-exist.bin"

    result = verify(source, dest, mode="size")
    assert not result.ok
    assert "missing" in result.error


def test_rsync_copy_reports_error_for_missing_source(tmp_path):
    source = tmp_path / "does-not-exist.bin"
    dest = tmp_path / "dest.bin"

    result = rsync_copy(source, dest)
    assert not result.ok
    # A missing source is permanent in our context (it never reappears),
    # despite rsync using the same exit code (23) for genuine transient
    # partial-transfer failures -- confirmed live while building this module.
    assert not result.transient
