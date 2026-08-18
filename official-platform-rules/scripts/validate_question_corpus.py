from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.clarify import clarify_question
from core.service import RuleService
from core.sources import SourceRejected, validate_official_url
from discover_official_sources import configured_identity


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def configured_identities(platform: str) -> set[str]:
    filename = "tiktok-us.json" if platform == "tiktok" else "ozon-crossborder.json"
    config = load_json(ROOT / "config" / filename)
    return {
        configured_identity(platform, str(source["url"]))
        for source in config["sources"]
    }


def validate_case(
    case: dict[str, Any],
    services: dict[str, RuleService],
    configured: dict[str, set[str]],
) -> dict[str, Any]:
    test_type = str(case["test_type"])
    platform = str(case["platform"])
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}

    if test_type == "positive_retrieval":
        rules = services[platform].db.search(
            str(case["question"]), limit=5, scope=case.get("scope", {})
        )
        expected_source = str(case["expected"]["source_key"])
        sources = [str(rule["source_key"]) for rule in rules]
        checks = {
            "has_official_evidence": bool(rules),
            "expected_source_top5": expected_source in sources,
            "official_url_present": all(bool(rule.get("source_url")) for rule in rules),
            "verified_at_present": all(bool(rule.get("verified_at")) for rule in rules),
        }
        details = {
            "expected_source": expected_source,
            "top_sources": sources,
            "top_titles": [rule["title"] for rule in rules],
            "top_scores": [rule.get("match_score") for rule in rules],
            "top1_match": bool(sources) and sources[0] == expected_source,
        }
    elif test_type == "coverage_gap":
        url = str(case["provenance"]["url"])
        identity = configured_identity(platform, url)
        checks = {
            "official_candidate_recorded": (
                case["provenance"].get("role")
                == "official_internal_link_not_yet_configured"
            ),
            "still_unconfigured": identity not in configured[platform],
            "snapshot_trace_present": bool(
                case["provenance"].get("discovered_from_snapshot")
            ),
        }
        details = {"candidate_url": url, "candidate_identity": identity}
    elif test_type == "scope_boundary":
        rules = services[platform].db.search(
            str(case["question"]), limit=5, scope=case.get("scope", {})
        )
        checks = {"no_cross_scope_results": not rules}
        details = {
            "unexpected_sources": [rule["source_key"] for rule in rules],
            "unexpected_titles": [rule["title"] for rule in rules],
        }
    elif test_type == "clarification":
        result = clarify_question(str(case["question"]))
        checks = {
            "needs_clarification": result["status"] == "needs_clarification",
            "asks_actionable_question": bool(result.get("questions")),
        }
        details = result
    elif test_type == "source_integrity":
        url = str(case["provided_url"])
        domains = (
            services[platform].config["official_domains"]
            if platform in services
            else []
        )
        rejected = False
        rejection = None
        try:
            validate_official_url(url, domains)
        except SourceRejected as exc:
            rejected = True
            rejection = str(exc)
        checks = {"non_official_url_rejected": rejected}
        details = {"provided_url": url, "rejection": rejection}
    elif test_type == "history_traceability":
        rule_key = str(case["expected"]["rule_key"])
        versions = services[platform].db.history(rule_key)
        checks = {
            "has_version": bool(versions),
            "source_url_present": all(bool(item.get("source_url")) for item in versions),
            "verified_at_present": all(bool(item.get("verified_at")) for item in versions),
            "status_present": all(bool(item.get("status")) for item in versions),
        }
        details = {
            "rule_key": rule_key,
            "version_count": len(versions),
            "statuses": [item.get("status") for item in versions],
        }
    else:
        raise ValueError(f"未知测试类型: {test_type}")

    return {
        "id": case["id"],
        "platform": platform,
        "test_type": test_type,
        "category": case["category"],
        "question": case["question"],
        "passed": all(checks.values()),
        "checks": checks,
        "details": details,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, Counter[str]] = defaultdict(Counter)
    by_platform: dict[str, Counter[str]] = defaultdict(Counter)
    for item in results:
        for bucket, key in (
            (by_type, item["test_type"]),
            (by_platform, item["platform"]),
        ):
            bucket[key]["total"] += 1
            bucket[key]["passed"] += int(item["passed"])
    positives = [item for item in results if item["test_type"] == "positive_retrieval"]
    top1 = sum(bool(item["details"].get("top1_match")) for item in positives)

    def materialize(values: dict[str, Counter[str]]) -> dict[str, Any]:
        return {
            key: {
                "total": counts["total"],
                "passed": counts["passed"],
                "failed": counts["total"] - counts["passed"],
                "pass_rate": counts["passed"] / max(1, counts["total"]),
            }
            for key, counts in sorted(values.items())
        }

    passed = sum(int(item["passed"]) for item in results)
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": passed / max(1, len(results)),
        "positive_top1_rate": top1 / max(1, len(positives)),
        "by_test_type": materialize(by_type),
        "by_platform": materialize(by_platform),
    }


def markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 1000 题知识库验证报告",
        "",
        f"- 运行时间（UTC）：{report['run_at']}",
        f"- 总题数：{summary['total']}",
        f"- 通过：{summary['passed']}",
        f"- 失败：{summary['failed']}",
        f"- 总通过率：{summary['pass_rate']:.1%}",
        f"- 正向召回 Top 1 命中率：{summary['positive_top1_rate']:.1%}",
        "",
        "## 分类结果",
        "",
        "| 类型 | 通过 | 总数 | 通过率 |",
        "|---|---:|---:|---:|",
    ]
    for key, item in summary["by_test_type"].items():
        lines.append(
            f"| {key} | {item['passed']} | {item['total']} | {item['pass_rate']:.1%} |"
        )
    lines.extend(
        [
            "",
            "## 平台结果",
            "",
            "| 平台 | 通过 | 总数 | 通过率 |",
            "|---|---:|---:|---:|",
        ]
    )
    for key, item in summary["by_platform"].items():
        lines.append(
            f"| {key} | {item['passed']} | {item['total']} | {item['pass_rate']:.1%} |"
        )
    lines.extend(["", "## 失败明细（最多列出 150 条）", ""])
    failures = [item for item in report["results"] if not item["passed"]]
    if not failures:
        lines.append("无失败。")
    else:
        lines.extend(
            [
                "| ID | 平台 | 类型 | 失败检查 | 问题 |",
                "|---|---|---|---|---|",
            ]
        )
        for item in failures[:150]:
            failed = ", ".join(
                name for name, passed in item["checks"].items() if not passed
            )
            question = str(item["question"]).replace("|", "\\|")
            lines.append(
                f"| {item['id']} | {item['platform']} | {item['test_type']} | "
                f"{failed} | {question} |"
            )
    lines.extend(
        [
            "",
            "## 判定说明",
            "",
            "- 正向召回：预期官方来源必须出现在 Top 5；Top 1 单独统计。",
            "- 覆盖缺口：必须可追溯到官方快照中的站内链接，且当前仍未配置。",
            "- 范围边界：错误市场、主体或履约方式不得返回其他范围的规则。",
            "- 模糊问题：必须返回 `needs_clarification` 并提出可执行的澄清问题。",
            "- 来源安全：伪官方域名、错误协议、userinfo 绕过必须被拒绝。",
            "- 历史追溯：规则必须至少有一个含 URL、核验时间和状态的版本。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 1000 题平台规则语料")
    parser.add_argument(
        "--corpus",
        type=Path,
        default=ROOT / "validation" / "question-corpus-1000-2026-07-26.json",
    )
    parser.add_argument("--date", default="2026-07-26")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    args = parser.parse_args()

    payload = load_json(args.corpus)
    services = {
        platform: RuleService(ROOT, platform) for platform in ("tiktok", "ozon")
    }
    for service in services.values():
        service.initialize()
    configured = {
        platform: configured_identities(platform) for platform in services
    }
    results = [
        validate_case(case, services, configured) for case in payload["cases"]
    ]
    report = {
        "schema_version": 2,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "corpus": str(args.corpus),
        "database_snapshots": {
            platform: {
                "schema_version": status["schema_version"],
                "database_revision": status["database_revision"],
                "last_sync_id": (
                    status["last_sync"]["id"]
                    if status.get("last_sync")
                    else None
                ),
            }
            for platform, service in services.items()
            for status in (service.status(),)
        },
        "summary": summarize(results),
        "results": results,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"question-corpus-validation-{args.date}.json"
    md_path = args.output_dir / f"question-corpus-validation-{args.date}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": report["summary"]["failed"] == 0,
                "summary": report["summary"],
                "json": str(json_path),
                "markdown": str(md_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
