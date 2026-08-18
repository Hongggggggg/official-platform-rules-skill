from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def audit_platform(platform: str, config_path: Path) -> dict[str, Any]:
    config = load_json(config_path)
    connection = sqlite3.connect(ROOT / "data" / platform / "rules.sqlite3")
    connection.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    try:
        rule_counts = {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM rule_versions GROUP BY status"
            )
        }
        source_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT s.*,
                       SUM(CASE WHEN r.status='current' THEN 1 ELSE 0 END) AS current_rules
                FROM sources s
                LEFT JOIN rule_versions r ON r.source_key=s.source_key
                GROUP BY s.source_key
                ORDER BY s.source_key
                """
            )
        ]
        unfinished = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, started_at, mode
                FROM sync_runs
                WHERE finished_at IS NULL
                ORDER BY id
                """
            )
        ]
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute(
                """
                SELECT key, value FROM metadata
                WHERE key IN ('schema_version', 'database_revision')
                """
            )
        }
        last_sync = connection.execute(
            """
            SELECT id, started_at, finished_at, mode, ok, database_revision
            FROM sync_runs ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()

    stale: list[dict[str, Any]] = []
    for row in source_rows:
        verified_at = row.get("last_verified_at")
        if not verified_at:
            continue
        risk = next(
            (
                source["risk"]
                for source in config["sources"]
                if source["source_key"] == row["source_key"]
            ),
            "normal",
        )
        threshold = config["freshness"][
            "high_risk_hours" if risk == "high" else "normal_hours"
        ]
        age = max(0.0, (now - parse_time(str(verified_at))).total_seconds() / 3600)
        if age > threshold:
            stale.append(
                {
                    "source_key": row["source_key"],
                    "risk": risk,
                    "age_hours": round(age, 2),
                    "threshold_hours": threshold,
                }
            )

    errors = [
        {
            "source_key": row["source_key"],
            "last_error": row["last_error"],
            "current_rules": int(row["current_rules"] or 0),
        }
        for row in source_rows
        if row.get("last_error")
    ]
    no_current = [
        {
            "source_key": row["source_key"],
            "last_verified_at": row["last_verified_at"],
            "last_error": row["last_error"],
        }
        for row in source_rows
        if int(row["current_rules"] or 0) == 0
    ]
    return {
        "platform": platform,
        "database_snapshot": {
            "schema_version": int(metadata.get("schema_version", "1")),
            "database_revision": int(metadata.get("database_revision", "0")),
            "last_sync": dict(last_sync) if last_sync else None,
        },
        "configured_sources": len(config["sources"]),
        "registered_sources": len(source_rows),
        "verified_sources": sum(bool(row.get("last_verified_at")) for row in source_rows),
        "sources_with_current_rules": sum(
            int(row["current_rules"] or 0) > 0 for row in source_rows
        ),
        "rule_counts": rule_counts,
        "source_errors": errors,
        "sources_without_current_rules": no_current,
        "stale_sources": stale,
        "unfinished_sync_runs": unfinished,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 平台规则知识库完整性审计",
        "",
        f"- 审计时间（UTC）：{report['generated_at']}",
        f"- 结论：**{report['conclusion']}**",
        "- “官方站内链接候选”来自已核验官方快照中的内部链接；它是可审计的扩展清单，不是官方全站的固定总数。",
        "",
        "## 当前证据库",
        "",
        "| 平台 | 已配置来源 | 已核验来源 | 有 current 规则的来源 | current 规则 | review_required | 来源错误 | 过期来源 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for platform, item in report["platforms"].items():
        lines.append(
            f"| {platform} | {item['configured_sources']} | {item['verified_sources']} | "
            f"{item['sources_with_current_rules']} | {item['rule_counts'].get('current', 0)} | "
            f"{item['rule_counts'].get('review_required', 0)} | "
            f"{len(item['source_errors'])} | {len(item['stale_sources'])} |"
        )
    discovery = report["official_link_discovery"]
    lines.extend(
        [
            "",
            "## 官方来源覆盖缺口",
            "",
            "| 平台 | 官方快照发现链接 | 已配置命中 | 未配置候选 |",
            "|---|---:|---:|---:|",
        ]
    )
    for platform, item in discovery["platforms"].items():
        lines.append(
            f"| {platform} | {item['discovered']} | {item['configured']} | "
            f"{item['unconfigured']} |"
        )
    lines.extend(
        [
            "",
            f"共发现 {discovery['total_discovered']} 个官方内部链接候选，"
            f"其中 {discovery['total_unconfigured']} 个尚未配置。",
            "",
            "### 未配置候选的主题分布",
            "",
            "| 平台 | 主题 | 未配置数 |",
            "|---|---|---:|",
        ]
    )
    for item in discovery["unconfigured_by_topic"]:
        lines.append(
            f"| {item['platform']} | {item['topic']} | {item['count']} |"
        )
    lines.extend(["", "## 数据质量问题", ""])
    issues = report["issues"]
    for issue in issues:
        lines.append(
            f"- [{issue['severity']}] {issue['code']}：{issue['message']}"
        )
    lines.extend(
        [
            "",
            "## 审计判断",
            "",
            "- 真实性：当前已入库事实均来自配置白名单中的官方域名，并保留 URL、快照和核验时间。",
            "- 准确性：1000 题验证另见专项报告；该结果证明当前题集的行为符合预期，但不能证明未知规则已全部覆盖。",
            "- 完整性：当前不能宣称完整。官方站内仍有大量未配置候选，并有少数动态页面抓取失败。",
            "- 持续性：应按风险等级定期复核；新发现链接需经官方域名、范围、时效和正文质量检查后再入库。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="审计平台规则知识库覆盖和证据健康")
    parser.add_argument(
        "--discovery",
        type=Path,
        default=ROOT / "reports" / "official-source-discovery-2026-07-26.json",
    )
    parser.add_argument("--date", default="2026-07-26")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    args = parser.parse_args()

    platforms = {
        "tiktok": audit_platform("tiktok", ROOT / "config" / "tiktok-us.json"),
        "ozon": audit_platform("ozon", ROOT / "config" / "ozon-crossborder.json"),
    }
    discovered = load_json(args.discovery)
    topic_counts: Counter[tuple[str, str]] = Counter(
        (str(item["platform"]), str(item["topic"]))
        for item in discovered["sources"]
        if not item["configured"]
    )
    unconfigured_topics = [
        {"platform": platform, "topic": topic, "count": count}
        for (platform, topic), count in sorted(
            topic_counts.items(), key=lambda item: (item[0][0], -item[1], item[0][1])
        )
    ]
    platform_discovery = discovered["summary"]["platforms"]
    total_discovered = sum(item["discovered"] for item in platform_discovery.values())
    total_unconfigured = sum(
        item["unconfigured"] for item in platform_discovery.values()
    )

    issues: list[dict[str, str]] = []
    for platform, item in platforms.items():
        if item["source_errors"]:
            issues.append(
                {
                    "severity": "high",
                    "code": f"{platform}.source_errors",
                    "message": (
                        f"{len(item['source_errors'])} 个来源最近抓取失败："
                        + ", ".join(row["source_key"] for row in item["source_errors"])
                    ),
                }
            )
        if item["stale_sources"]:
            issues.append(
                {
                    "severity": "high",
                    "code": f"{platform}.stale_sources",
                    "message": f"{len(item['stale_sources'])} 个来源超过其风险时效阈值。",
                }
            )
        if item["unfinished_sync_runs"]:
            issues.append(
                {
                    "severity": "medium",
                    "code": f"{platform}.unfinished_sync_runs",
                    "message": (
                        f"{len(item['unfinished_sync_runs'])} 次同步因外部中断未写入结束状态。"
                    ),
                }
            )
    issues.append(
        {
            "severity": "high",
            "code": "coverage.unconfigured_official_candidates",
            "message": f"官方快照中仍有 {total_unconfigured} 个站内链接候选未配置。",
        }
    )

    report = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "conclusion": "not_complete_but_evidence_backed",
        "platforms": platforms,
        "official_link_discovery": {
            "source_report": str(args.discovery),
            "total_discovered": total_discovered,
            "total_unconfigured": total_unconfigured,
            "platforms": platform_discovery,
            "unconfigured_by_topic": unconfigured_topics,
        },
        "issues": issues,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"knowledge-base-completeness-audit-{args.date}.json"
    md_path = args.output_dir / f"knowledge-base-completeness-audit-{args.date}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "conclusion": report["conclusion"],
                "platforms": {
                    key: {
                        "configured_sources": value["configured_sources"],
                        "verified_sources": value["verified_sources"],
                        "current_rules": value["rule_counts"].get("current", 0),
                        "source_errors": len(value["source_errors"]),
                        "stale_sources": len(value["stale_sources"]),
                        "unfinished_sync_runs": len(value["unfinished_sync_runs"]),
                    }
                    for key, value in platforms.items()
                },
                "official_candidates": {
                    "discovered": total_discovered,
                    "unconfigured": total_unconfigured,
                },
                "json": str(json_path),
                "markdown": str(md_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
