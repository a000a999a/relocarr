import os

from relocarr.transfer.health import probe_destination


def test_probe_succeeds_for_a_writable_directory_with_room(tmp_path):
    result = probe_destination(tmp_path)
    assert result.ok, result.error
    # the probe file must not be left behind
    assert list(tmp_path.iterdir()) == []


def test_probe_fails_for_nonexistent_directory(tmp_path):
    result = probe_destination(tmp_path / "does-not-exist")
    assert not result.ok
    assert "does not exist" in result.error


def test_probe_fails_for_unwritable_directory(tmp_path):
    if os.geteuid() == 0:
        return  # root bypasses permission checks -- can't test this as root
    target = tmp_path / "readonly"
    target.mkdir(mode=0o500)
    try:
        result = probe_destination(target)
        assert not result.ok
    finally:
        target.chmod(0o700)  # so tmp_path cleanup can remove it


def test_probe_fails_when_below_minimum_free_space(tmp_path):
    # An absurdly high minimum that no real filesystem will satisfy --
    # exercises the free-space branch deterministically without needing to
    # actually fill a disk.
    result = probe_destination(tmp_path, min_free_bytes=10**18)
    assert not result.ok
    assert "free" in result.error
