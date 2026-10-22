import pytest

from relocarr.transfer.layout import compute_target_path, sidecar_paths
from relocarr.transfer.safety import PathSafetyError


@pytest.fixture
def staging(tmp_path):
    root = tmp_path / "staging" / "movies"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def target(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    return root


def test_flat_layout_drops_folder_structure(staging, target):
    movie_dir = staging / "Big File Movie2026"
    movie_dir.mkdir()
    source = movie_dir / "Big File Movie.2026.mkv"
    source.touch()

    result = compute_target_path(
        source_path=source, staging_root=staging, target_root=target, mode="flat"
    )
    assert result == (target / "Big File Movie.2026.mkv").resolve()


def test_preserve_layout_keeps_relative_path(staging, target):
    # Mirrors Sonarr's <Series>/<Season N>/<file> shape (Section 7, fixed layout).
    episode_dir = staging / "Show B" / "Season 1"
    episode_dir.mkdir(parents=True)
    source = episode_dir / "Show B - S01E01.mkv"
    source.touch()

    result = compute_target_path(
        source_path=source, staging_root=staging, target_root=target, mode="preserve"
    )
    assert result == (target / "Show B" / "Season 1" / "Show B - S01E01.mkv").resolve()


def test_rejects_unknown_mode(staging, target):
    source = staging / "file.mkv"
    source.touch()
    with pytest.raises(ValueError):
        compute_target_path(source_path=source, staging_root=staging, target_root=target, mode="series-folder")


def test_sidecar_paths_matches_same_stem_and_extension(staging):
    movie_dir = staging / "Sidecar Movie2001"
    movie_dir.mkdir()
    main = movie_dir / "Sidecar Movie.2001.mp4"
    srt = movie_dir / "Sidecar Movie.2001.srt"
    nfo = movie_dir / "Sidecar Movie.2001.nfo"
    other_stem = movie_dir / "other.srt"
    for f in (main, srt, nfo, other_stem):
        f.touch()

    result = sidecar_paths(main, extensions=["srt"])
    assert result == [srt]


def test_compute_target_path_works_when_target_root_does_not_exist(staging, tmp_path):
    # Reproduces the real production bug, 2026-10-22: the destination (a
    # NAS the user deliberately powers off most of the time) wasn't
    # mounted, so target_root didn't exist on disk. A job must still be
    # creatable from the webhook in this case -- the destination's actual
    # availability is checked for real at copy time instead (Section 12).
    movie_dir = staging / "Big File Movie2026"
    movie_dir.mkdir()
    source = movie_dir / "Big File Movie.2026.mkv"
    source.touch()

    missing_target = tmp_path / "mnt" / "nvidia" / "movies"  # never created
    assert not missing_target.exists()

    result = compute_target_path(
        source_path=source, staging_root=staging, target_root=missing_target, mode="flat"
    )
    assert result == (missing_target / "Big File Movie.2026.mkv").resolve(strict=False)
