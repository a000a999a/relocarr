import pytest

from relocarr.transfer.safety import PathSafetyError, assert_distinct_roots, resolve_within


def test_resolve_within_accepts_path_inside_root(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    target = root / "movie" / "file.mp4"
    assert resolve_within(target, root) == target.resolve()


def test_resolve_within_rejects_path_outside_root(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "file.mp4"
    with pytest.raises(PathSafetyError):
        resolve_within(outside, root)


def test_resolve_within_rejects_symlink_escape(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    escape_link = root / "escape"
    escape_link.symlink_to(outside_dir)
    with pytest.raises(PathSafetyError):
        resolve_within(escape_link / "file.mp4", root)


def test_assert_distinct_roots_rejects_same_path(tmp_path):
    with pytest.raises(PathSafetyError):
        assert_distinct_roots(tmp_path, tmp_path)


def test_assert_distinct_roots_rejects_nesting(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    with pytest.raises(PathSafetyError):
        assert_distinct_roots(tmp_path, inner)
    with pytest.raises(PathSafetyError):
        assert_distinct_roots(inner, tmp_path)


def test_assert_distinct_roots_accepts_siblings(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert_distinct_roots(a, b)  # must not raise
