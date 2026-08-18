from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TABLES = {
    "applicability_scopes",
    "effective_intervals",
    "evidence_links",
    "extracted_sections",
    "fetch_runs",
    "migration_history",
    "review_decisions",
    "rule_versions",
    "rules_fts_v2",
    "snapshots",
    "sources",
    "sync_runs",
}
NOISE_TITLES = {
    "похоже, нет соединения",
    "просмотр статей",
    "article viewer",
    "no internet connection",
}


def scalar(connection: sqlite3.Connection, sql: str) -> Any:
    return connection.execute(sql).fetchone()[0]


def verify(platform: str) -> dict[str, Any]:
    path = ROOT / "data" / platform / "rules.sqlite3"
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute(
                """
                SELECT key, value FROM metadata
                WHERE key IN ('schema_version', 'database_revision', 'fts5_v2')
                """
            )
        }
        current_titles = [
            str(row["title"]).strip().casefold()
            for row in connection.execute(
                "SELECT title FROM rule_versions WHERE status='current'"
            )
        ]
        checks = {
            "integrity": scalar(connection, "PRAGMA integrity_check") == "ok",
            "schema_version": metadata.get("schema_version") == "2",
            "fts5_v2_enabled": metadata.get("fts5_v2") == "true",
            "required_tables": not (REQUIRED_TABLES - tables),
            "current_scope_complete": scalar(
                connection,
                """
                SELECT COUNT(*) FROM rule_versions
                WHERE status='current' AND scope_id IS NULL
                """,
            )
            == 0,
            "current_interval_complete": scalar(
                connection,
                """
                SELECT COUNT(*) FROM rule_versions AS rv
                LEFT JOIN effective_intervals AS ei
                  ON ei.rule_version_id=rv.id
                WHERE rv.status='current' AND ei.id IS NULL
                """,
            )
            == 0,
            "current_evidence_complete": scalar(
                connection,
                """
                SELECT COUNT(*) FROM rule_versions AS rv
                LEFT JOIN evidence_links AS el
                  ON el.rule_version_id=rv.id
                WHERE rv.status='current' AND el.id IS NULL
                """,
            )
            == 0,
            "snapshot_raw_hash_complete": scalar(
                connection,
                """
                SELECT COUNT(*) FROM snapshots
                WHERE raw_content_hash IS NULL OR raw_content_hash=''
                """,
            )
            == 0,
            "fts_rule_coverage": scalar(
                connection, "SELECT COUNT(DISTINCT rule_id) FROM rules_fts_v2"
            )
            >= scalar(connection, "SELECT COUNT(*) FROM rule_versions"),
            "no_current_noise_shells": not any(
                title in NOISE_TITLES for title in current_titles
            ),
            "no_running_fetches": scalar(
                connection,
                "SELECT COUNT(*) FROM fetch_runs WHERE outcome='running'",
            )
            == 0,
        }
        counts = {
            table: int(scalar(connection, f"SELECT COUNT(*) FROM {table}"))
            for table in (
                "sources",
                "snapshots",
                "extracted_sections",
                "rule_versions",
                "effective_intervals",
                "evidence_links",
                "fetch_runs",
                "review_decisions",
            )
        }
    finally:
        connection.close()
    return {
        "platform": platform,
        "database": str(path),
        "database_revision": int(metadata.get("database_revision", "0")),
        "ok": all(checks.values()),
        "checks": checks,
        "missing_tables": sorted(REQUIRED_TABLES - tables),
        "counts": counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="验证平台规则知识库 V2")
    parser.add_argument(
        "--platform",
        action="append",
        choices=("tiktok", "ozon"),
        help="可重复；省略时验证所有已启用平台",
    )
    args = parser.parse_args()
    platforms = args.platform or ["tiktok", "ozon"]
    results = [verify(platform) for platform in platforms]
    payload = {"schema_version": 2, "ok": all(item["ok"] for item in results), "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
