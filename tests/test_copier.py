import shutil

import pytest

from relocarr.transfer.copier import rsync_copy, verify

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


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
