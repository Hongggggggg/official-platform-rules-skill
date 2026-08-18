from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = 2
PARSER_VERSION = "html-parser-v2"
SCOPE_FIELDS = (
    "market",
    "seller_origin",
    "actor_type",
    "seller_type",
    "shop_type",
    "fulfillment",
    "category",
    "program",
    "order_state",
)


V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_history (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applicability_scopes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'all',
    seller_origin TEXT NOT NULL DEFAULT 'all',
    actor_type TEXT NOT NULL DEFAULT 'seller',
    seller_type TEXT NOT NULL DEFAULT 'all',
    shop_type TEXT NOT NULL DEFAULT 'all',
    fulfillment TEXT NOT NULL DEFAULT 'all',
    category TEXT NOT NULL DEFAULT 'all',
    program TEXT NOT NULL DEFAULT 'all',
    order_state TEXT NOT NULL DEFAULT 'all',
    account_scoped INTEGER NOT NULL DEFAULT 0,
    qualifiers_json TEXT NOT NULL DEFAULT '{}',
    scope_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_run_id INTEGER REFERENCES sync_runs(id),
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT NOT NULL DEFAULT 'running' CHECK (
        outcome IN ('running', 'changed', 'unchanged', 'not_modified', 'error')
    ),
    http_status INTEGER,
    final_url TEXT,
    etag TEXT,
    last_modified TEXT,
    content_hash TEXT,
    snapshot_id INTEGER REFERENCES snapshots(id),
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetch_runs_source
ON fetch_runs(source_key, id DESC);

CREATE TABLE IF NOT EXISTS extracted_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    section_key TEXT NOT NULL,
    heading TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    parser_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(snapshot_id, section_key, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_sections_snapshot
ON extracted_sections(snapshot_id, ordinal);

CREATE TABLE IF NOT EXISTS effective_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_version_id INTEGER NOT NULL UNIQUE REFERENCES rule_versions(id),
    valid_from TEXT,
    valid_to TEXT,
    observed_at TEXT NOT NULL,
    retired_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_effective_lookup
ON effective_intervals(valid_from, valid_to);

CREATE TABLE IF NOT EXISTS evidence_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_version_id INTEGER NOT NULL REFERENCES rule_versions(id),
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    section_id INTEGER REFERENCES extracted_sections(id),
    source_url TEXT NOT NULL,
    fragment TEXT,
    content_hash TEXT NOT NULL,
    linked_at TEXT NOT NULL,
    UNIQUE(rule_version_id, snapshot_id, section_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_rule
ON evidence_links(rule_version_id, id DESC);

CREATE TABLE IF NOT EXISTS review_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_version_id INTEGER NOT NULL REFERENCES rule_versions(id),
    decision TEXT NOT NULL CHECK (
        decision IN ('approve', 'reject', 'withdraw', 'keep_current')
    ),
    reason TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    notes TEXT,
    decided_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_decisions_rule
ON review_decisions(rule_version_id, id DESC);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _add_column(
    connection: sqlite3.Connection, table: str, name: str, declaration: str
) -> None:
    if name not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def canonical_scope(
    platform: str,
    scope: dict[str, Any] | None,
    account_scoped: bool = False,
) -> dict[str, Any]:
    supplied = scope or {}
    result: dict[str, Any] = {
        "platform": platform,
        "market": "all",
        "seller_origin": "all",
        "actor_type": "seller",
        "seller_type": "all",
        "shop_type": "all",
        "fulfillment": "all",
        "category": "all",
        "program": "all",
        "order_state": "all",
        "account_scoped": int(account_scoped),
    }
    qualifiers: dict[str, str] = {}
    for key, value in supplied.items():
        normalized = str(value).strip() or "all"
        if key in SCOPE_FIELDS:
            result[key] = normalized
        else:
            qualifiers[key] = normalized
    result["qualifiers_json"] = json.dumps(
        qualifiers, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest_payload = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    result["scope_hash"] = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
    return result


def upsert_scope(
    connection: sqlite3.Connection,
    platform: str,
    scope: dict[str, Any] | None,
    account_scoped: bool = False,
) -> int:
    value = canonical_scope(platform, scope, account_scoped)
    columns = (
        "platform",
        *SCOPE_FIELDS,
        "account_scoped",
        "qualifiers_json",
        "scope_hash",
    )
    connection.execute(
        f"""
        INSERT OR IGNORE INTO applicability_scopes(
            {", ".join(columns)}, created_at
        ) VALUES({", ".join("?" for _ in columns)}, ?)
        """,
        tuple(value[column] for column in columns) + (utc_now(),),
    )
    row = connection.execute(
        "SELECT id FROM applicability_scopes WHERE scope_hash=?",
        (value["scope_hash"],),
    ).fetchone()
    if row is None:
        raise RuntimeError("无法建立适用范围记录")
    return int(row[0])


def _cjk_bigrams(value: str) -> list[str]:
    chunks = re.findall(r"[\u3400-\u9fff]+", value)
    result: list[str] = []
    for chunk in chunks:
        if len(chunk) == 1:
            result.append(chunk)
        else:
            result.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return result


def normalize_search_text(value: str) -> str:
    latin = re.findall(r"[a-z0-9][a-z0-9_-]*", value.lower())
    cyrillic = re.findall(r"[\u0400-\u04ff]{2,}", value.lower())
    cjk = _cjk_bigrams(value)
    return " ".join(dict.fromkeys((*latin, *cyrillic, *cjk)))


def _create_fts(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS rules_fts_v2
            USING fts5(
                rule_id UNINDEXED,
                title,
                content,
                topic,
                normalized,
                tokenize='unicode61'
            )
            """
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('fts5_v2', 'true')"
        )
        return True
    except sqlite3.OperationalError:
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('fts5_v2', 'false')"
        )
        return False


def _backfill_scopes(connection: sqlite3.Connection, platform: str) -> None:
    source_rows = connection.execute(
        """
        SELECT source_key, market, seller_type, fulfillment, account_scoped
        FROM sources
        """
    ).fetchall()
    for row in source_rows:
        scope_id = upsert_scope(
            connection,
            platform,
            {
                "market": row["market"],
                "seller_type": row["seller_type"],
                "fulfillment": row["fulfillment"],
            },
            bool(row["account_scoped"]),
        )
        connection.execute(
            "UPDATE sources SET scope_id=? WHERE source_key=?",
            (scope_id, row["source_key"]),
        )
    connection.execute(
        """
        UPDATE rule_versions
        SET scope_id=(
            SELECT scope_id FROM sources
            WHERE sources.source_key=rule_versions.source_key
        )
        WHERE scope_id IS NULL
        """
    )


def _backfill_intervals(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE rule_versions
        SET valid_from=COALESCE(
                valid_from, effective_at, published_at, substr(created_at, 1, 10)
            ),
            observed_at=COALESCE(observed_at, created_at, verified_at)
        """
    )
    successors = connection.execute(
        """
        SELECT supersedes_id,
               COALESCE(effective_at, published_at, substr(created_at, 1, 10)) AS boundary
        FROM rule_versions
        WHERE supersedes_id IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    for row in successors:
        connection.execute(
            """
            UPDATE rule_versions
            SET valid_to=COALESCE(valid_to, ?)
            WHERE id=?
            """,
            (row["boundary"], row["supersedes_id"]),
        )
    connection.execute(
        """
        INSERT OR IGNORE INTO effective_intervals(
            rule_version_id, valid_from, valid_to, observed_at, retired_at, created_at
        )
        SELECT id, valid_from, valid_to, observed_at,
               CASE WHEN status IN ('withdrawn', 'superseded') THEN valid_to END,
               COALESCE(observed_at, created_at)
        FROM rule_versions
        """
    )


def _backfill_evidence(connection: sqlite3.Connection) -> None:
    rules = connection.execute(
        """
        SELECT id, rule_key, source_key, title, content, content_hash,
               verified_at, source_url
        FROM rule_versions
        WHERE section_id IS NULL
        ORDER BY id
        """
    ).fetchall()
    for rule in rules:
        snapshot = connection.execute(
            """
            SELECT id FROM snapshots
            WHERE source_key=? AND fetched_at<=?
            ORDER BY fetched_at DESC, id DESC LIMIT 1
            """,
            (rule["source_key"], rule["verified_at"]),
        ).fetchone()
        if snapshot is None:
            snapshot = connection.execute(
                """
                SELECT id FROM snapshots
                WHERE source_key=? ORDER BY fetched_at DESC, id DESC LIMIT 1
                """,
                (rule["source_key"],),
            ).fetchone()
        if snapshot is None:
            continue
        connection.execute(
            """
            INSERT OR IGNORE INTO extracted_sections(
                snapshot_id, section_key, heading, content, content_hash,
                ordinal, parser_version, created_at
            ) VALUES(?, ?, ?, ?, ?, 0, 'legacy-v1', ?)
            """,
            (
                snapshot["id"],
                rule["rule_key"],
                rule["title"],
                rule["content"],
                rule["content_hash"],
                rule["verified_at"],
            ),
        )
        section = connection.execute(
            """
            SELECT id FROM extracted_sections
            WHERE snapshot_id=? AND section_key=? AND ordinal=0
            """,
            (snapshot["id"], rule["rule_key"]),
        ).fetchone()
        if section is None:
            continue
        connection.execute(
            "UPDATE rule_versions SET section_id=? WHERE id=?",
            (section["id"], rule["id"]),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO evidence_links(
                rule_version_id, snapshot_id, section_id, source_url,
                fragment, content_hash, linked_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule["id"],
                snapshot["id"],
                section["id"],
                rule["source_url"],
                rule["title"],
                rule["content_hash"],
                rule["verified_at"],
            ),
        )


def _backfill_fts(connection: sqlite3.Connection) -> None:
    if not _create_fts(connection):
        return
    indexed = {
        int(row[0])
        for row in connection.execute("SELECT rule_id FROM rules_fts_v2").fetchall()
    }
    rules = connection.execute(
        "SELECT id, title, content, topic FROM rule_versions ORDER BY id"
    ).fetchall()
    for row in rules:
        rule_id = int(row["id"])
        if rule_id in indexed:
            continue
        normalized = normalize_search_text(
            f"{row['title']} {row['topic']} {row['content']}"
        )
        connection.execute(
            """
            INSERT INTO rules_fts_v2(rule_id, title, content, topic, normalized)
            VALUES(?, ?, ?, ?, ?)
            """,
            (rule_id, row["title"], row["content"], row["topic"], normalized),
        )


def migrate_v2(connection: sqlite3.Connection, platform: str) -> dict[str, Any]:
    connection.executescript(V2_SCHEMA)
    already_applied = connection.execute(
        "SELECT 1 FROM migration_history WHERE version=?",
        (SCHEMA_VERSION,),
    ).fetchone() is not None

    _add_column(connection, "sources", "scope_id", "INTEGER")
    _add_column(connection, "sources", "etag", "TEXT")
    _add_column(connection, "sources", "last_modified", "TEXT")
    _add_column(connection, "sources", "last_http_status", "INTEGER")
    _add_column(connection, "sources", "last_fetch_run_id", "INTEGER")

    _add_column(connection, "snapshots", "raw_content_hash", "TEXT")
    _add_column(connection, "snapshots", "raw_bytes", "INTEGER")
    _add_column(connection, "snapshots", "fetch_run_id", "INTEGER")
    _add_column(
        connection,
        "snapshots",
        "parser_version",
        f"TEXT NOT NULL DEFAULT '{PARSER_VERSION}'",
    )

    _add_column(connection, "rule_versions", "scope_id", "INTEGER")
    _add_column(connection, "rule_versions", "section_id", "INTEGER")
    _add_column(connection, "rule_versions", "valid_from", "TEXT")
    _add_column(connection, "rule_versions", "valid_to", "TEXT")
    _add_column(connection, "rule_versions", "observed_at", "TEXT")

    _add_column(
        connection,
        "sync_runs",
        "schema_version",
        f"INTEGER NOT NULL DEFAULT {SCHEMA_VERSION}",
    )
    _add_column(connection, "sync_runs", "source_keys_json", "TEXT")
    _add_column(connection, "sync_runs", "database_revision", "INTEGER")

    if not already_applied:
        connection.execute(
            """
            UPDATE snapshots
            SET raw_content_hash=COALESCE(raw_content_hash, content_hash),
                raw_bytes=COALESCE(raw_bytes, 0),
                parser_version=COALESCE(parser_version, 'legacy-v1')
            """
        )
        _backfill_scopes(connection, platform)
        _backfill_intervals(connection)
        _backfill_evidence(connection)
        _backfill_fts(connection)
    else:
        _create_fts(connection)

    connection.execute(
        """
        INSERT OR IGNORE INTO metadata(key, value)
        VALUES('database_revision', '1')
        """
    )
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
                {
                    "name": "temporal-evidence-scope-search-v2",
                    "platform": platform,
                },
                ensure_ascii=False,
            ),
        ),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "already_applied": already_applied,
        "fts5_v2": (
            connection.execute(
                "SELECT value FROM metadata WHERE key='fts5_v2'"
            ).fetchone()[0]
            == "true"
        ),
    }
