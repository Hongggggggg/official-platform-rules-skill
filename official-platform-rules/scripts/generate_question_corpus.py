from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATE = datetime.now(timezone.utc).date().isoformat()

WEB_QUESTION_SEEDS = [
    {
        "url": "https://www.reddit.com/r/TikTokshop/comments/1u6flop/appealing_late_dispatch_violations_wherehow_do_i/",
        "theme": "延迟发货申诉与证明材料",
        "role": "question_wording_only",
    },
    {
        "url": "https://www.reddit.com/r/TikTokshop/comments/1rdhtay/200k_store_now_stuck_with_10_ordersday_limit_need/",
        "theme": "订单量限制与无申诉入口",
        "role": "question_wording_only",
    },
    {
        "url": "https://www.reddit.com/r/TikTokshop/comments/1ti0f46/customer_filed_missing_package_claim_ai_support/",
        "theme": "包裹未收到与物流索赔",
        "role": "question_wording_only",
    },
    {
        "url": "https://www.reddit.com/r/TikTokshop/comments/1tw73l8/new_returns_update_your_return_shipping_costs_are/",
        "theme": "退货运费与政策变化",
        "role": "question_wording_only",
    },
    {
        "url": "https://www.reddit.com/r/Tiktokhelp/comments/17p2klq",
        "theme": "注册信息违规与申诉材料",
        "role": "question_wording_only",
    },
    {
        "url": "https://seller-us.tiktok.com/university/essay?knowledge_id=8794641096984333&lang=en",
        "theme": "评价请求合规",
        "role": "official_faq_question_seed",
    },
    {
        "url": "https://seller-us.tiktok.com/university/essay?knowledge_id=462733062424334",
        "theme": "退货退款常见问题",
        "role": "official_faq_question_seed",
    },
    {
        "url": "https://seller-us.tiktok.com/university/essay?knowledge_id=3158393393481486&lang=en",
        "theme": "24小时回复率",
        "role": "official_faq_question_seed",
    },
    {
        "url": "https://www.reddit.com/r/TikTokshop/comments/1t00vls/tiktok_shop_permanently_suspended/",
        "theme": "无违规明细但店铺被暂停",
        "role": "question_wording_only",
    },
    {
        "url": "https://www.reddit.com/r/TikTokshop/comments/1q3vdnm/tiktok_shop_permanently_withheld_my_seller_funds/",
        "theme": "关店后货款冻结与释放时间",
        "role": "question_wording_only",
    },
    {
        "url": "https://www.reddit.com/r/TikTokshop/comments/1s6r9aw/tiktok_shop_is_banning_accounts_for_violations/",
        "theme": "尚未销售即触发资质或关联风险处罚",
        "role": "question_wording_only",
    },
    {
        "url": "https://www.reddit.com/r/TikTokshop/comments/1lv85si/tiktok_shop_suspended_my_account/",
        "theme": "正常履约后暂停且申诉立即失败",
        "role": "question_wording_only",
    },
    {
        "url": "https://www.reddit.com/r/TikTokShopSellers/comments/1smcjat/tiktok_shop_permanently_suspended_after_linking/",
        "theme": "绑定账号后重新验证身份并暂停",
        "role": "question_wording_only",
    },
    {
        "url": "https://www.reddit.com/r/TikTokshop/comments/1udfm3g/banned_from_tiktok_shop_twice_first_account_was/",
        "theme": "多个账户关联与二次申诉",
        "role": "question_wording_only",
    },
    {
        "url": "https://www.reddit.com/r/TikTokshop/comments/1t1kuwn/tiktok_shop_manual_escalation_violation_appeal/",
        "theme": "包裹损坏、售后与延迟结算处罚",
        "role": "question_wording_only",
    },
    {
        "url": "https://www.reddit.com/r/AskARussian/comments/1r5ddku/ozon_sent_me_a_box_of_paper_instead_of_a_351828/",
        "theme": "Ozon 错货空包、退货拒绝与争议升级",
        "role": "question_wording_only",
    },
    {
        "url": "https://seller-us.tiktok.com/university/essay?knowledge_id=1238551019980586",
        "theme": "如何判断达人推广的商品是否禁售",
        "role": "official_faq_question_seed",
    },
    {
        "url": "https://seller-us.tiktok.com/university/essay?knowledge_id=1707918207813422",
        "theme": "类目资质申请与高风险类目",
        "role": "official_faq_question_seed",
    },
    {
        "url": "https://seller-us.tiktok.com/university/essay?knowledge_id=3638385374070542",
        "theme": "如何纠正卖家违规",
        "role": "official_faq_question_seed",
    },
    {
        "url": "https://seller-us.tiktok.com/university/essay?knowledge_id=491489038501663",
        "theme": "AI 生成内容限制与披露",
        "role": "official_faq_question_seed",
    },
    {
        "url": "https://seller-us.tiktok.com/university/essay?knowledge_id=248891033372462",
        "theme": "个人卖家或独资企业入驻文件",
        "role": "official_faq_question_seed",
    },
    {
        "url": "https://seller-us.tiktok.com/university/essay?knowledge_id=1903367874381610",
        "theme": "负余额处理要求",
        "role": "official_faq_question_seed",
    },
]

POSITIVE_TARGETS = {"tiktok": 400, "ozon": 150}
GAP_TARGETS = {"tiktok": 100, "ozon": 100}
SCOPE_TARGETS = {"tiktok": 50, "ozon": 50}
CLARIFICATION_TARGET = 80
SOURCE_INTEGRITY_TARGET = 40
HISTORY_TARGET = 30
TOTAL_TARGET = 1000

NOISY_TITLES = {
    "ai summary",
    "contents",
    "directory",
    "enforcement actions and appeals",
    "overview",
    "sections",
    "table of contents",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def rules(platform: str) -> list[dict[str, Any]]:
    path = ROOT / "data" / platform / "rules.sqlite3"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT id, rule_key, source_key, title, content, topic, market,
                   seller_type, fulfillment, source_url, source_type,
                   published_at, effective_at, verified_at
            FROM rule_versions
            WHERE status='current'
            ORDER BY source_key, id
            """
        ).fetchall()
    finally:
        connection.close()
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        item = dict(row)
        title = re.sub(r"\s+", " ", str(item["title"])).strip()
        key = (str(item["source_key"]), title.casefold())
        if (
            key in seen
            or title.casefold() in NOISY_TITLES
            or title.casefold().startswith(("next ", "previous "))
            or len(title) < 4
            or len(title) > 180
            or len(str(item["content"])) < 120
        ):
            continue
        seen.add(key)
        item["title"] = title
        result.append(item)
    return result


def round_robin(items: list[dict[str, Any]], group_key: str) -> Iterable[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[str(item[group_key])].append(item)
    keys = sorted(groups)
    index = 0
    while any(groups.values()):
        key = keys[index % len(keys)]
        if groups[key]:
            yield groups[key].pop(0)
        index += 1


def platform_label(platform: str) -> str:
    return "TikTok Shop 美国站" if platform == "tiktok" else "Ozon 中国跨境卖家"


def scope_for_rule(rule: dict[str, Any]) -> dict[str, str]:
    return {
        "market": str(rule["market"]),
        "seller_type": str(rule["seller_type"]),
        "fulfillment": str(rule["fulfillment"]),
    }


def positive_templates(platform: str, rule: dict[str, Any]) -> list[tuple[str, str]]:
    label = platform_label(platform)
    title = rule["title"]
    topic = rule["topic"]
    identity = rule["seller_type"]
    return [
        ("zh", f"{label}对“{title}”的当前官方要求是什么？"),
        ("en", f"What are the current official requirements for “{title}” on {label}?"),
        ("zh", f"请只依据官方一手来源说明“{title}”，并给出适用范围和最后核验时间。"),
        ("en", f"For a {identity}, what does the official rule say about “{title}”?"),
        ("zh", f"关于{topic}，规则章节“{title}”具体规定了什么？"),
        ("en", f"Please cite the official source, scope, and effective date for “{title}”."),
        ("zh", f"如果遇到“{title}”相关问题，平台规则、例外和可执行动作分别是什么？"),
        ("en", f"Does the policy section “{title}” apply to this seller scope, and what must be done?"),
    ]


def add_unique(cases: list[dict[str, Any]], seen: set[str], item: dict[str, Any]) -> bool:
    question = re.sub(r"\s+", " ", item["question"]).strip()
    if question in seen:
        return False
    item["question"] = question
    item["id"] = f"q{len(cases) + 1:04d}"
    seen.add(question)
    cases.append(item)
    return True


def add_positive_cases(
    cases: list[dict[str, Any]], seen: set[str], platform: str, target: int
) -> None:
    selected = list(round_robin(rules(platform), "source_key"))
    if not selected:
        raise RuntimeError(f"{platform} 没有可用 current 规则")
    added = 0
    round_index = 0
    while added < target:
        rule = selected[round_index % len(selected)]
        templates = positive_templates(platform, rule)
        language, question = templates[(round_index // len(selected)) % len(templates)]
        if add_unique(
            cases,
            seen,
            {
                "platform": platform,
                "test_type": "positive_retrieval",
                "category": str(rule["topic"]),
                "language": language,
                "question": question,
                "scope": scope_for_rule(rule),
                "expected": {
                    "behavior": "confirm_with_official_evidence",
                    "source_key": rule["source_key"],
                    "rule_key": rule["rule_key"],
                },
                "provenance": {
                    "role": "official_rule_heading_and_body",
                    "url": rule["source_url"],
                    "verified_at": rule["verified_at"],
                },
            },
        ):
            added += 1
        round_index += 1
        if round_index > target * 20:
            raise RuntimeError(f"{platform} 正向问题无法达到 {target}")


def clean_candidate_title(title: str) -> str | None:
    value = re.sub(r"\s+", " ", title).strip()
    if (
        len(value) < 4
        or len(value) > 160
        or value.casefold().startswith(("next ", "previous "))
        or value.casefold() in NOISY_TITLES
    ):
        return None
    return value


def add_gap_cases(
    cases: list[dict[str, Any]],
    seen: set[str],
    platform: str,
    target: int,
    discovery: dict[str, Any],
) -> None:
    candidates = []
    for item in discovery["sources"]:
        title = clean_candidate_title(str(item["title"]))
        if item["platform"] == platform and not item["configured"] and title:
            candidates.append({**item, "title": title})
    candidates.sort(key=lambda item: (item["topic"], item["title"].casefold(), item["url"]))
    if not candidates:
        raise RuntimeError(f"{platform} 没有未配置官方候选来源")
    templates = (
        ("zh", lambda label, title: f"{label}关于“{title}”的官方规则是什么？当前知识库能否给出可追溯依据？"),
        ("en", lambda label, title: f"What official guidance does {label} publish about “{title}”, and is it covered by the knowledge base?"),
        ("zh", lambda label, title: f"请核验“{title}”是否已有当前有效官方来源入库；若没有，请明确标记缺口。"),
    )
    added = 0
    index = 0
    while added < target:
        candidate = candidates[index % len(candidates)]
        language, render = templates[(index // len(candidates)) % len(templates)]
        if add_unique(
            cases,
            seen,
            {
                "platform": platform,
                "test_type": "coverage_gap",
                "category": candidate["topic"],
                "language": language,
                "question": render(platform_label(platform), candidate["title"]),
                "scope": (
                    {"market": "US", "seller_type": "seller"}
                    if platform == "tiktok"
                    else {
                        "market": "RU_CIS",
                        "seller_type": "cross_border",
                        "fulfillment": "FBP_OR_REALFBS",
                    }
                ),
                "expected": {
                    "behavior": "identify_unconfigured_official_source",
                    "configured_at_generation": False,
                },
                "provenance": {
                    "role": "official_internal_link_not_yet_configured",
                    "url": candidate["url"],
                    "discovered_from_snapshot": candidate["discovered_from_snapshot"],
                },
            },
        ):
            added += 1
        index += 1
        if index > target * 20:
            raise RuntimeError(f"{platform} 缺口问题无法达到 {target}")


def add_scope_cases(
    cases: list[dict[str, Any]], seen: set[str], platform: str, target: int
) -> None:
    selected = list(round_robin(rules(platform), "source_key"))
    bad_scope = (
        {"market": "UK", "seller_type": "seller", "fulfillment": "all"}
        if platform == "tiktok"
        else {"market": "TR", "seller_type": "local", "fulfillment": "FBO"}
    )
    label = "TikTok Shop 英国站" if platform == "tiktok" else "Ozon 土耳其本土卖家"
    added = 0
    index = 0
    while added < target:
        rule = selected[index % len(selected)]
        variant = index // len(selected) + 1
        question = (
            f"{label}是否适用“{rule['title']}”这条规则？"
            f"这是范围边界测试场景 {variant}，请不要套用其他市场或主体的规则。"
        )
        if add_unique(
            cases,
            seen,
            {
                "platform": platform,
                "test_type": "scope_boundary",
                "category": str(rule["topic"]),
                "language": "zh",
                "question": question,
                "scope": bad_scope,
                "expected": {"behavior": "refuse_scope_leakage"},
                "provenance": {
                    "role": "official_rule_used_as_cross_scope_adversarial_seed",
                    "url": rule["source_url"],
                    "correct_scope": scope_for_rule(rule),
                },
            },
        ):
            added += 1
        index += 1


def add_clarification_cases(cases: list[dict[str, Any]], seen: set[str]) -> None:
    objects = ("鞋子", "食品", "锂电池", "保健品", "化妆品", "二手商品", "酒类", "玩具", "医疗器械", "定制商品")
    patterns = (
        "这款{item}在平台上能卖吗？",
        "{item}需要什么资质才能上架？",
        "卖{item}被下架了，为什么？",
        "{item}订单被退款了该怎么办？",
        "{item}发货要多久？",
        "{item}违规后怎么申诉？",
        "{item}可以做直播推广吗？",
        "{item}被限制流量是什么规则？",
    )
    added = 0
    for item in objects:
        for pattern in patterns:
            if add_unique(
                cases,
                seen,
                {
                    "platform": "unspecified",
                    "test_type": "clarification",
                    "category": "模糊问题澄清",
                    "language": "zh",
                    "question": pattern.format(item=item),
                    "scope": {},
                    "expected": {"behavior": "needs_clarification"},
                    "provenance": {
                        "role": "web_question_pattern_synthesis",
                        "seed_sources": [seed["url"] for seed in WEB_QUESTION_SEEDS],
                    },
                },
            ):
                added += 1
    if added != CLARIFICATION_TARGET:
        raise RuntimeError(f"澄清问题预期 {CLARIFICATION_TARGET}，实际 {added}")


def add_source_integrity_cases(cases: list[dict[str, Any]], seen: set[str]) -> None:
    fake_urls = []
    for host in (
        "seller-us.tiktok.com.evil.example",
        "tiktokshop-policy.example",
        "docs.ozon.com.evil.example",
        "ozon-seller-help.example",
        "seller-us-tiktok.com",
    ):
        for scheme in ("https", "http"):
            fake_urls.append(f"{scheme}://{host}/policy/{len(fake_urls) + 1}")
    fake_urls.extend(
        [
            "https://seller-us.tiktok.com@evil.example/policy",
            "https://docs.ozon.com@evil.example/rules",
            "https://sub.seller-us.tiktok.com.evil.example/essay",
            "https://global-help.ozon.com.evil.example/en/rules",
            "ftp://seller-us.tiktok.com/policy",
        ]
    )
    index = 0
    while len([c for c in cases if c["test_type"] == "source_integrity"]) < SOURCE_INTEGRITY_TARGET:
        url = fake_urls[index % len(fake_urls)]
        platform = "tiktok" if "tiktok" in url else "ozon"
        variant = index // len(fake_urls) + 1
        add_unique(
            cases,
            seen,
            {
                "platform": platform,
                "test_type": "source_integrity",
                "category": "伪官方域名与协议",
                "language": "zh",
                "question": f"这个链接能作为第{variant}组官方规则证据吗：{url}",
                "scope": {},
                "provided_url": url,
                "expected": {"behavior": "reject_non_official_url"},
                "provenance": {"role": "adversarial_security_test"},
            },
        )
        index += 1


def add_history_cases(cases: list[dict[str, Any]], seen: set[str]) -> None:
    selected = [
        *list(round_robin(rules("tiktok"), "source_key"))[:20],
        *list(round_robin(rules("ozon"), "source_key"))[:10],
    ]
    for rule in selected:
        platform = "ozon" if "ozon" in rule["source_url"] else "tiktok"
        add_unique(
            cases,
            seen,
            {
                "platform": platform,
                "test_type": "history_traceability",
                "category": str(rule["topic"]),
                "language": "zh",
                "question": f"“{rule['title']}”这条规则有哪些版本、何时核验，当前版本状态是什么？",
                "scope": scope_for_rule(rule),
                "expected": {
                    "behavior": "return_version_history",
                    "rule_key": rule["rule_key"],
                },
                "provenance": {
                    "role": "official_rule_history_seed",
                    "url": rule["source_url"],
                },
            },
        )


def build_corpus(discovery_path: Path) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    discovery = load_json(discovery_path)
    for platform, target in POSITIVE_TARGETS.items():
        add_positive_cases(cases, seen, platform, target)
    for platform, target in GAP_TARGETS.items():
        add_gap_cases(cases, seen, platform, target, discovery)
    for platform, target in SCOPE_TARGETS.items():
        add_scope_cases(cases, seen, platform, target)
    add_clarification_cases(cases, seen)
    add_source_integrity_cases(cases, seen)
    add_history_cases(cases, seen)
    if len(cases) != TOTAL_TARGET or len(seen) != TOTAL_TARGET:
        raise RuntimeError(
            f"题集必须正好 {TOTAL_TARGET} 条且唯一，实际 cases={len(cases)}, unique={len(seen)}"
        )
    counts: dict[str, int] = defaultdict(int)
    for item in cases:
        counts[item["test_type"]] += 1
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "description": (
            "1000-question official platform rules validation corpus. "
            "Third-party pages are used only to diversify wording, never as rule evidence."
        ),
        "evidence_policy": {
            "rule_evidence": "first_party_official_only",
            "third_party_role": "question_wording_only",
            "search_snippets_are_evidence": False,
        },
        "web_question_seed_sources": WEB_QUESTION_SEEDS,
        "summary": {"total": len(cases), "by_test_type": dict(sorted(counts.items()))},
        "cases": cases,
    }


def markdown_overview(payload: dict[str, Any]) -> str:
    lines = [
        "# 1000 条平台规则验证问题集",
        "",
        f"- 生成时间（UTC）：{payload['generated_at']}",
        f"- 问题总数：{payload['summary']['total']}",
        "- 规则事实证据：仅平台官方一手来源。",
        "- 第三方网页：只用于模拟真实用户问法，不采用其中任何规则断言。",
        "",
        "## 分类",
        "",
        "| 测试类型 | 数量 | 验证目标 |",
        "|---|---:|---|",
    ]
    descriptions = {
        "positive_retrieval": "已入库官方规则能否召回正确来源",
        "coverage_gap": "官方站内存在但未配置的来源能否被识别为缺口",
        "scope_boundary": "市场/主体/履约范围是否串用",
        "clarification": "缺少平台、市场或主题时是否先澄清",
        "source_integrity": "伪官方域名、错误协议和用户信息段攻击是否拒绝",
        "history_traceability": "规则版本、状态、核验时间与 URL 是否可追溯",
    }
    for key, value in payload["summary"]["by_test_type"].items():
        lines.append(f"| {key} | {value} | {descriptions[key]} |")
    lines.extend(
        [
            "",
            "## 自动验证口径",
            "",
            "- 正向召回：Top 5 中必须出现预期官方来源。",
            "- 覆盖缺口：候选必须来自已核验官方快照的站内链接，且生成时未配置。",
            "- 范围边界：用错误市场/主体检索时不得返回规则。",
            "- 澄清：协议必须返回 `needs_clarification`。",
            "- 来源安全：非 HTTPS 或伪官方域名必须被官方 URL 校验器拒绝。",
            "- 历史追溯：至少存在一个版本，且含官方 URL、状态和核验时间。",
            "",
            "完整逐题数据见同名 JSON 文件。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=DEFAULT_DATE)
    parser.add_argument(
        "--discovery",
        type=Path,
        default=ROOT / "reports" / f"official-source-discovery-{DEFAULT_DATE}.json",
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "validation")
    args = parser.parse_args()
    payload = build_corpus(args.discovery)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"question-corpus-1000-{args.date}.json"
    md_path = args.output_dir / f"question-corpus-1000-{args.date}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown_overview(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "total": payload["summary"]["total"],
                "by_test_type": payload["summary"]["by_test_type"],
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
