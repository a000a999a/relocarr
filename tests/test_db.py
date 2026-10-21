from pathlib import Path

from relocarr.db import DEFAULT_SETTINGS, init_db, session


def test_init_db_creates_schema_and_defaults(tmp_path: Path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)

    with session(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"settings", "apps", "download_clients", "downloads", "jobs"} <= tables

        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        settings = {r["key"]: r["value"] for r in rows}
        assert settings == DEFAULT_SETTINGS


def test_init_db_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "relocarr.db"
    init_db(db_path)
    init_db(db_path)  # must not raise or duplicate default settings

    with session(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM settings").fetchone()["n"]
        assert count == len(DEFAULT_SETTINGS)
