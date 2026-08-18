from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.profiles import ProfileStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将因进程外部中断而没有结束标记的同步记录结算为失败"
    )
    parser.add_argument(
        "--profile", action="append",
        help="可重复；省略时处理所有未归档档案",
    )
    args = parser.parse_args()
    store = ProfileStore(ROOT)
    platforms = args.profile or [
        item["profile_id"] for item in store.list()
        if item.get("status") != "archived"
    ]
    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reconciled: dict[str, list[int]] = {}
    for platform in platforms:
        path = store.profile_dir(platform) / "rules.sqlite3"
        if not path.exists():
            reconciled[platform] = []
            continue
        connection = sqlite3.connect(path)
        try:
            revision_row = connection.execute(
                "SELECT value FROM metadata WHERE key='database_revision'"
            ).fetchone()
            revision = int(revision_row[0]) if revision_row else 0
            ids = [
                int(row[0])
                for row in connection.execute(
                    "SELECT id FROM sync_runs WHERE finished_at IS NULL ORDER BY id"
                )
            ]
            for run_id in ids:
                result = {
                    "ok": False,
                    "reconciled": True,
                    "reason": "external_process_interruption",
                    "note": "原同步进程未写入结束状态；未删除其已提交的逐来源结果。",
                }
                connection.execute(
                    """
                    UPDATE sync_runs
                    SET finished_at=?, ok=0, result_json=?,
                        schema_version=3, database_revision=?
                    WHERE id=? AND finished_at IS NULL
                    """,
                    (
                        finished_at,
                        json.dumps(result, ensure_ascii=False),
                        revision,
                        run_id,
                    ),
                )
            connection.commit()
            reconciled[platform] = ids
        finally:
            connection.close()
    print(
        json.dumps(
            {
                "ok": True,
                "finished_at": finished_at,
                "reconciled_run_ids": reconciled,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
