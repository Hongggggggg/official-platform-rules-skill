from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.config import load_platform_config
from core.service import RuleService


def normalize(value: str) -> str:
    return " ".join(
        value.lower()
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u00d7", "x")
        .split()
    )


def scope_matches(rule_value: str, requested: str) -> bool:
    left = rule_value.lower()
    right = requested.lower()
    return left == "all" or left == right or left in right or right in left


def official_host(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def evaluate_variant(
    service: RuleService,
    case: dict[str, Any],
    question: str,
    domains: list[str],
) -> dict[str, Any]:
    expected = case["expected"]
    result = service.query(
        question,
        limit=int(expected.get("top_k", 3)),
        scope=case.get("scope", {}),
        refresh_stale=False,
    )
    rules = result["rules"]
    expected_confirmed = bool(expected["confirmed"])
    checks: dict[str, bool] = {
        "confirmation": bool(result["official_evidence_confirmed"]) == expected_confirmed,
        "noise_free": all(
            normalize(rule["title"])
            not in {"table of contents", "contents", "sections", "directory"}
            for rule in rules
        ),
        "official_domains": all(
            official_host(rule["source_url"], domains) for rule in rules
        ),
        "scope": all(
            all(
                scope_matches(str(rule[field]), str(value))
                for field, value in case.get("scope", {}).items()
                if field in {"market", "seller_type", "fulfillment"}
            )
            for rule in rules
        ),
    }
    top_sources = [rule["source_key"] for rule in rules]
    top_titles = [rule["title"] for rule in rules]
    top_rule_keys = [rule["rule_key"] for rule in rules]

    if expected_confirmed:
        expected_sources = expected.get("sources", [])
        expected_titles = expected.get("titles", [])
        checks["has_results"] = bool(rules)
        checks["source_top_k"] = not expected_sources or any(
            source in expected_sources for source in top_sources
        )
        checks["source_top_1"] = not expected_sources or (
            bool(top_sources) and top_sources[0] in expected_sources
        )
        checks["title_top_k"] = not expected_titles or any(
            any(normalize(wanted) in normalize(actual) for wanted in expected_titles)
            for actual in top_titles
        )
        relevant = [
            rule
            for rule in rules
            if not expected_sources or rule["source_key"] in expected_sources
        ]
        combined = normalize("\n".join(rule["content"] for rule in relevant))
        checks["content_assertions"] = all(
            normalize(fragment) in combined
            for fragment in expected.get("must_contain", [])
        )
    else:
        checks["rejected"] = not rules and not result["official_evidence_confirmed"]

    blocking_checks = [
        value
        for name, value in checks.items()
        if name != "source_top_1"
    ]
    passed = all(blocking_checks)
    return {
        "case_id": case["id"],
        "platform": case["platform"],
        "question": question,
        "scope": case.get("scope", {}),
        "expected_confirmed": expected_confirmed,
        "actual_confirmed": bool(result["official_evidence_confirmed"]),
        "passed": passed,
        "checks": checks,
        "top_sources": top_sources,
        "top_titles": top_titles,
        "top_rule_keys": top_rule_keys,
        "top_scores": [rule.get("match_score") for rule in rules],
        "match_reasons": [rule.get("match_reasons", []) for rule in rules],
    }


def markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Official Platform Rules Query Validation",
        "",
        f"- Run at: {report['run_at']}",
        f"- Query variants: {summary['total_queries']}",
        f"- Passed: {summary['passed']} ({summary['pass_rate']:.1%})",
        f"- Positive retrieval: {summary['positive_passed']}/{summary['positive_queries']}",
        f"- Unsupported-query rejection: {summary['negative_passed']}/{summary['negative_queries']}",
        f"- Expected source at top 1: {summary['top1_source_rate']:.1%}",
        f"- Expected source in top 3: {summary['topk_source_rate']:.1%}",
        f"- Content assertion rate: {summary['content_assertion_rate']:.1%}",
        "",
        "## Platform breakdown",
        "",
        "| Platform | Passed | Total | Pass rate |",
        "|---|---:|---:|---:|",
    ]
    for platform, item in sorted(report["platforms"].items()):
        lines.append(
            f"| {platform} | {item['passed']} | {item['total']} | {item['pass_rate']:.1%} |"
        )
    lines.extend(["", "## Evidence health", ""])
    for platform, health in sorted(report["evidence_health"].items()):
        lines.extend(
            [
                f"### {platform}",
                "",
                f"- Configured sources: {health['configured_sources']}",
                f"- Current rules: {health['current_rules']}",
                f"- Review required: {health['review_required']}",
                f"- Oldest verification age: {health['oldest_verified_age_hours']:.2f} hours",
                f"- Covered expected sources: {', '.join(health['covered_sources']) or 'none'}",
                "",
            ]
        )
    lines.extend(["## Failures", ""])
    failures = [item for item in report["results"] if not item["passed"]]
    if not failures:
        lines.append("No blocking failures.")
    else:
        lines.append("| Case | Platform | Question | Failed checks | Top results |")
        lines.append("|---|---|---|---|---|")
        for item in failures:
            failed = ", ".join(
                name for name, value in item["checks"].items()
                if not value and name != "source_top_1"
            )
            top = " / ".join(item["top_titles"][:3]) or "none"
            question = item["question"].replace("|", "\\|")
            lines.append(
                f"| {item['case_id']} | {item['platform']} | {question} | {failed} | {top} |"
            )
    lines.extend(
        [
            "",
            "## Known evidence boundary",
            "",
            "The negative cases are intentional. A clean rejection means the knowledge base did not convert a nearby US-local or general product rule into a claim about an unsupported market, seller type, or legal certification.",
            "",
        ]
    )
    return "\n".join(lines)


def run(cases_path: Path, output_dir: Path) -> dict[str, Any]:
    payload = json.loads(cases_path.read_text(encoding="utf-8"))
    services: dict[str, RuleService] = {}
    domains: dict[str, list[str]] = {}
    results: list[dict[str, Any]] = []
    expected_source_coverage: dict[str, set[str]] = defaultdict(set)

    for case in payload["cases"]:
        platform = case["platform"]
        if platform not in services:
            services[platform] = RuleService(SKILL_ROOT, platform)
            services[platform].initialize()
            domains[platform] = services[platform].config["official_domains"]
        expected_source_coverage[platform].update(
            case["expected"].get("sources", [])
        )
        for question in case["queries"]:
            results.append(
                evaluate_variant(services[platform], case, question, domains[platform])
            )

    positive = [item for item in results if item["expected_confirmed"]]
    negative = [item for item in results if not item["expected_confirmed"]]
    source_checks = [item["checks"].get("source_top_k", True) for item in positive]
    top1_checks = [item["checks"].get("source_top_1", True) for item in positive]
    content_checks = [
        item["checks"].get("content_assertions", True) for item in positive
    ]
    summary = {
        "total_queries": len(results),
        "passed": sum(item["passed"] for item in results),
        "failed": sum(not item["passed"] for item in results),
        "pass_rate": sum(item["passed"] for item in results) / max(1, len(results)),
        "positive_queries": len(positive),
        "positive_passed": sum(item["passed"] for item in positive),
        "negative_queries": len(negative),
        "negative_passed": sum(item["passed"] for item in negative),
        "top1_source_rate": sum(top1_checks) / max(1, len(top1_checks)),
        "topk_source_rate": sum(source_checks) / max(1, len(source_checks)),
        "content_assertion_rate": sum(content_checks) / max(1, len(content_checks)),
    }

    platform_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for item in results:
        platform_counts[item["platform"]]["total"] += 1
        platform_counts[item["platform"]]["passed"] += int(item["passed"])
    platforms = {
        platform: {
            "total": counts["total"],
            "passed": counts["passed"],
            "pass_rate": counts["passed"] / max(1, counts["total"]),
        }
        for platform, counts in platform_counts.items()
    }

    now = datetime.now(timezone.utc)
    health: dict[str, Any] = {}
    for platform, service in services.items():
        status = service.status()
        verified = [
            datetime.fromisoformat(item["last_verified_at"].replace("Z", "+00:00"))
            for item in status["sources"]
            if item.get("last_verified_at")
        ]
        oldest = max(
            ((now - stamp).total_seconds() / 3600 for stamp in verified),
            default=0.0,
        )
        health[platform] = {
            "schema_version": status["schema_version"],
            "database_revision": status["database_revision"],
            "last_sync_id": (
                status["last_sync"]["id"] if status.get("last_sync") else None
            ),
            "configured_sources": len(status["sources"]),
            "current_rules": status["rule_counts"].get("current", 0),
            "review_required": status["rule_counts"].get("review_required", 0),
            "oldest_verified_age_hours": oldest,
            "covered_sources": sorted(expected_source_coverage[platform]),
        }

    report = {
        "schema_version": 2,
        "run_at": now.replace(microsecond=0).isoformat(),
        "cases_file": str(cases_path),
        "summary": summary,
        "platforms": platforms,
        "evidence_health": health,
        "results": results,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.date().isoformat()
    json_path = output_dir / f"query-validation-{stamp}.json"
    md_path = output_dir / f"query-validation-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    report["output_json"] = str(json_path)
    report["output_markdown"] = str(md_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate official-rule retrieval")
    parser.add_argument(
        "--cases",
        type=Path,
        default=SKILL_ROOT / "validation" / "query_cases.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SKILL_ROOT / "reports",
    )
    args = parser.parse_args()
    report = run(args.cases.resolve(), args.output_dir.resolve())
    print(json.dumps({
        "summary": report["summary"],
        "platforms": report["platforms"],
        "evidence_health": report["evidence_health"],
        "output_json": report["output_json"],
        "output_markdown": report["output_markdown"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
