from relocarr.clients.base import Torrent


def _torrent(**overrides) -> Torrent:
    defaults = dict(
        id=1, name="t", hash="abc", percent_done=1.0, status=0, download_dir="/d"
    )
    defaults.update(overrides)
    return Torrent(**defaults)


def test_is_complete_true_at_full_percent_even_if_not_finished():
    # Confirmed live via spike 4 (2026-10-21): isFinished stays false even at
    # percentDone=1.0/status=stopped. Completion must be judged by percentDone
    # alone -- this test guards against ever wiring isFinished back in.
    t = _torrent(percent_done=1.0, status=0)
    assert t.is_complete is True


def test_is_complete_false_while_downloading():
    t = _torrent(percent_done=0.42, status=4)
    assert t.is_complete is False
