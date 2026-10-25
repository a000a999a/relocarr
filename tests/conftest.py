import pytest

from relocarr.disk_health import cache as disk_space_cache
from relocarr.library import cache as movie_library_cache
from relocarr.plex import cache as plex_quality_cache
from relocarr.version_check import cache as version_check_cache


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


@pytest.fixture(autouse=True)
def _reset_disk_space_cache():
    """Same reasoning as _reset_movie_library_cache: relocarr.disk_health.cache
    is a module-level singleton the sidebar badge and /log /system both
    read on every render.
    """
    disk_space_cache.entries = {}
    disk_space_cache.checked_at = None
    disk_space_cache.last_timeout_at = None
    yield
    disk_space_cache.entries = {}
    disk_space_cache.checked_at = None
    disk_space_cache.last_timeout_at = None


@pytest.fixture(autouse=True)
def _reset_version_check_cache():
    """Same reasoning as the other two caches above: relocarr.version_check.cache
    is a module-level singleton the System page reads on every render.
    """
    version_check_cache.latest_version = None
    version_check_cache.checked_at = None
    version_check_cache.error = None
    yield
    version_check_cache.latest_version = None
    version_check_cache.checked_at = None
    version_check_cache.error = None


@pytest.fixture(autouse=True)
def _reset_plex_quality_cache():
    """Same reasoning as the other caches above: relocarr.plex.cache is a
    module-level singleton /log/duplicates reads on every render.
    """
    plex_quality_cache.index = {}
    plex_quality_cache.checked_at = None
    plex_quality_cache.error = None
    yield
    plex_quality_cache.index = {}
    plex_quality_cache.checked_at = None
    plex_quality_cache.error = None
