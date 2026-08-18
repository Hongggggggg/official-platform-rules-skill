from __future__ import annotations

import json
import sqlite3
from typing import Any

from .schema_v2 import utc_now


SCHEMA_VERSION = 3


V3_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT PRIMARY KEY,
    platform_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    market TEXT NOT NULL,
    seller_origin TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    seller_type TEXT NOT NULL,
    fulfillment TEXT NOT NULL,
    timezone TEXT NOT NULL,
    daily_update_time TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    ok INTEGER,
    result_json TEXT
);

CREATE TABLE IF NOT EXISTS source_candidates (
    url TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    domain TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL,
    risk TEXT NOT NULL,
    source_type TEXT NOT NULL,
    provenance TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_candidates_status
ON source_candidates(status, topic);

CREATE TABLE IF NOT EXISTS coverage_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audited_at TEXT NOT NULL,
    status TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS update_schedules (
    profile_id TEXT PRIMARY KEY REFERENCES profiles(profile_id),
    timezone TEXT NOT NULL,
    daily_update_time TEXT NOT NULL,
    daily_enabled INTEGER NOT NULL DEFAULT 1,
    weekly_rediscovery_day INTEGER NOT NULL DEFAULT 6,
    last_update_at TEXT,
    last_discovery_at TEXT,
    updated_at TEXT NOT NULL
);
"""


def migrate_v3(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.executescript(V3_SCHEMA)
    already_applied = connection.execute(
        "SELECT 1 FROM migration_history WHERE version=?", (SCHEMA_VERSION,)
    ).fetchone() is not None
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO migration_history(version, applied_at, details_json)
        VALUES(?, ?, ?)
        """,
        (
            SCHEMA_VERSION,
            utc_now(),
            json.dumps(
                {"name": "dynamic-platform-profiles-and-discovery"},
                ensure_ascii=False,
            ),
        ),
    )
    return {"schema_version": SCHEMA_VERSION, "already_applied": already_applied}
