import logging
import logging.handlers

import pytest

from relocarr.config import Settings
from relocarr.main import configure_logging


@pytest.fixture(autouse=True)
def _restore_root_logger_handlers():
    """configure_logging() uses force=True, replacing the root logger's
    handlers process-wide -- without restoring them afterward, a test's
    TimedRotatingFileHandler would keep pointing at that test's tmp_path
    (deleted once the test ends) for the rest of the whole test session,
    and every later logger.info() call anywhere in the suite would try to
    write to a now-gone file.
    """
    original = logging.getLogger().handlers[:]
    yield
    for handler in logging.getLogger().handlers:
        if handler not in original:
            handler.close()
    logging.getLogger().handlers[:] = original


def test_settings_log_path_lives_under_config_dir(tmp_path):
    test_settings = Settings(config_dir=tmp_path)
    assert test_settings.log_path == tmp_path / "logs" / "relocarr.log"


def test_configure_logging_creates_the_log_directory_and_file(tmp_path):
    test_settings = Settings(config_dir=tmp_path)
    configure_logging(test_settings)

    assert test_settings.log_path.parent.is_dir()


def test_configure_logging_writes_log_records_to_the_file(tmp_path):
    test_settings = Settings(config_dir=tmp_path)
    configure_logging(test_settings)

    logger = logging.getLogger("relocarr.test_main")
    logger.info("hello from test_configure_logging")
    for handler in logging.getLogger().handlers:
        handler.flush()

    content = test_settings.log_path.read_text()
    assert "hello from test_configure_logging" in content


def test_configure_logging_uses_a_timed_rotating_handler_with_seven_day_retention(tmp_path):
    test_settings = Settings(config_dir=tmp_path)
    configure_logging(test_settings)

    file_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert file_handlers[0].backupCount == 7
    assert file_handlers[0].when == "MIDNIGHT"
