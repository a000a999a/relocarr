import sqlite3
from pathlib import Path

from relocarr.db import DEFAULT_SETTINGS, connect, init_db, session


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


def test_init_db_adds_min_redownload_reduction_pct_to_a_pre_existing_apps_table(tmp_path: Path):
    # Simulates a real production database created before this column
    # existed -- CREATE TABLE IF NOT EXISTS is a no-op against an apps
    # table that's already there, so only an explicit ALTER TABLE
    # migration (db.py's _ensure_column) can add it without losing data.
    db_path = tmp_path / "relocarr.db"
    conn = connect(db_path)
    conn.execute(
        """CREATE TABLE apps (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               kind TEXT NOT NULL,
               name TEXT NOT NULL,
               base_url TEXT NOT NULL,
               api_key TEXT NOT NULL,
               webhook_token TEXT NOT NULL,
               staging_root TEXT NOT NULL,
               target_root TEXT NOT NULL
           )"""
    )
    conn.execute(
        """INSERT INTO apps (kind, name, base_url, api_key, webhook_token, staging_root, target_root)
           VALUES ('radarr', 'Radarr', 'http://x', 'key', 'token', '/s', '/t')"""
    )
    conn.commit()
    conn.close()

    init_db(db_path)  # must not raise, and must preserve the existing row

    with session(db_path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(apps)").fetchall()}
        assert "min_redownload_reduction_pct" in columns

        row = conn.execute("SELECT * FROM apps WHERE name = 'Radarr'").fetchone()
        assert row is not None
        assert row["min_redownload_reduction_pct"] is None
