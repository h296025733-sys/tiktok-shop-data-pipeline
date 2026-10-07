from __future__ import annotations

import sqlite3
from pathlib import Path

from src.config import PROJECT_ROOT


STATE_DB = PROJECT_ROOT / "data" / "state" / "state.sqlite3"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_jobs (
    job_id TEXT PRIMARY KEY,
    shop_id TEXT,
    dataset TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    cursor TEXT,
    last_success_at TEXT,
    rows_received INTEGER NOT NULL DEFAULT 0,
    rows_written INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error_code TEXT,
    error_summary TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    source_path TEXT,
    export_path TEXT
);

CREATE TABLE IF NOT EXISTS import_snapshots (
    source_sha256 TEXT NOT NULL,
    source_sheet TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    normalized_path TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    PRIMARY KEY (source_sha256, source_sheet, snapshot_date)
);
"""


def initialize_state_db(path: Path = STATE_DB) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("SELECT 1")
    return path


def state_db_is_healthy(path: Path = STATE_DB) -> bool:
    try:
        initialize_state_db(path)
        with sqlite3.connect(path) as connection:
            return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False

