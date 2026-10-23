import pytest

from relocarr.library import cache as movie_library_cache


@pytest.fixture(autouse=True)
def _reset_movie_library_cache():
    """The movie library scan cache (relocarr.library.cache) is a
    module-level singleton shared by the real background scan loop and by
    every page's sidebar badge (base.html calls sidebar_issues() on every
    render, via the Jinja global in routes.py) -- reset it around every
    test in the whole suite so one test's cache state can never leak into
    another's assertions, regardless of which file either lives in.
    """
    movie_library_cache.files = []
    movie_library_cache.scanned_at = None
    movie_library_cache.unreachable_roots = []
    movie_library_cache.last_timeout_at = None
    yield
    movie_library_cache.files = []
    movie_library_cache.scanned_at = None
    movie_library_cache.unreachable_roots = []
    movie_library_cache.last_timeout_at = None
