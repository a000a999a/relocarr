import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS apps (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                 TEXT NOT NULL CHECK (kind IN ('radarr', 'sonarr')),
    name                 TEXT NOT NULL,
    base_url             TEXT NOT NULL,
    api_key              TEXT NOT NULL,
    webhook_token        TEXT NOT NULL,   -- X-Relocarr-Token the *arr's webhook must send (Section 10)
    staging_root         TEXT NOT NULL,
    target_root          TEXT NOT NULL,
    layout_mode          TEXT NOT NULL DEFAULT 'flat' CHECK (layout_mode IN ('flat', 'preserve')),
    max_file_size_bytes  INTEGER,                   -- NULL/0 = unlimited; never hardcoded (2026-10-21)
    conflict_policy      TEXT NOT NULL DEFAULT 'skip' CHECK (conflict_policy IN ('skip', 'replace', 'keep_both')),
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS download_clients (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id             INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL DEFAULT 'transmission',
    host               TEXT NOT NULL,
    port               INTEGER NOT NULL,
    username           TEXT,
    password            TEXT,
    stop_on_completion INTEGER NOT NULL DEFAULT 1,
    remove_after_copy  INTEGER NOT NULL DEFAULT 1
);

-- One row per (app, downloadId) the moment it's seen complete and stopped.
-- Recorded before any job exists -- see PLAN.md Section 5, state SEALED.
CREATE TABLE IF NOT EXISTS downloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    download_id TEXT NOT NULL,   -- torrent hash; compared case-insensitively (spike 4)
    sealed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (app_id, download_id COLLATE NOCASE)
);

-- One row per imported media file. download_id is nullable: manual imports
-- and anything not driven by the tracked download queue have none (spike 2)
-- and skip the removal gate entirely.
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id        INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    download_id   TEXT,
    media_file_id TEXT NOT NULL,      -- the *arr's movieFile/episodeFile id
    unmonitor_id  INTEGER NOT NULL,   -- movie id (Radarr) or episode id (Sonarr, Section 6)
    source_path   TEXT NOT NULL,      -- staging path
    target_path   TEXT NOT NULL,      -- computed destination path
    size_bytes    INTEGER NOT NULL,
    state         TEXT NOT NULL DEFAULT 'IMPORTED' CHECK (state IN (
                      'IMPORTED', 'COPYING', 'VERIFIED', 'PUBLISHED',
                      'RECONCILED', 'CLEANED', 'DONE', 'NEEDS_ATTENTION'
                  )),
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- Duplicate webhooks (retries) are de-duplicated by (app, fileId) -- Section 12.
    -- A genuine future upgrade gets a new movieFile/episodeFile id from the *arr,
    -- so this never blocks a real re-import, only a retried delivery of the same one.
    UNIQUE (app_id, media_file_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_download_id ON jobs(download_id COLLATE NOCASE);
"""

DEFAULT_SETTINGS = {
    "dry_run": "true",
    "poll_interval_seconds": "15",
    "sidecar_extensions": "srt",
    "verify_mode": "size",  # 'size' | 'checksum'
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def session(db_path: Path):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
