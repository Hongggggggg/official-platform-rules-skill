from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.profiles import ProfileStore

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
    "profiles",
    "discovery_runs",
    "source_candidates",
    "coverage_audits",
    "update_schedules",
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
    path = ROOT / "data" / "profiles" / platform / "rules.sqlite3"
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
            "schema_version": metadata.get("schema_version") == "3",
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
    parser = argparse.ArgumentParser(description="验证动态平台规则知识库 V3")
    parser.add_argument(
        "--platform",
        action="append",
        help="可重复；省略时验证所有未归档的运行时档案",
    )
    args = parser.parse_args()
    store = ProfileStore(ROOT)
    platforms = args.platform or [
        item["profile_id"] for item in store.list()
        if item.get("status") != "archived" and (store.profile_dir(item["profile_id"]) / "rules.sqlite3").exists()
    ]
    results = [verify(platform) for platform in platforms]
    payload = {
        "schema_version": 3,
        "ok": all(item["ok"] for item in results),
        "results": results,
        "warning": None if results else "尚无已构建的运行时平台数据库",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
