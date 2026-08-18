from __future__ import annotations

"""Compatibility helpers for legacy fixtures; production discovery is profile-driven."""

import argparse
import html
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

from core.profiles import ProfileStore
from core.service import RuleService


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATE = datetime.now(timezone.utc).date().isoformat()


TOPIC_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("合同与入驻", ("contract", "terms", "offer", "sign up", "register", "onboarding", "account")),
    ("禁限售与资质", ("prohibited", "restricted", "certificate", "qualification", "safety data", "brand")),
    ("商品与上架", ("product", "listing", "pdp", "image", "video", "barcode", "moderation", "sku")),
    ("内容与达人", ("creator", "affiliate", "content", "live", "shoppable", "community")),
    ("订单与履约", ("fulfillment", "shipping", "delivery", "warehouse", "order", "stock", "packaging")),
    ("退款退货", ("return", "refund", "replacement", "exchange", "dispute", "cancellation")),
    ("费用与结算", ("fee", "tariff", "finance", "payment", "commission", "currency", "expense")),
    ("价格与促销", ("price", "promotion", "discount", "coupon", "advertising", "ads")),
    ("绩效处罚与申诉", ("rating", "metric", "blocking", "enforcement", "appeal", "violation", "health")),
    ("客户沟通与数据", ("communication", "chat", "review", "question", "privacy", "data")),
)


@dataclass(frozen=True)
class Link:
    href: str
    text: str


class AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attr_map = dict(attrs)
        self._href = attr_map.get("href")
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        text = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
        self.links.append(Link(self._href, text))
        self._href = None
        self._parts = []


def read_config(platform: str) -> dict:
    filename = "tiktok-us.json" if platform == "tiktok" else "ozon-crossborder.json"
    return json.loads((ROOT / "config" / filename).read_text(encoding="utf-8-sig"))


def configured_identity(platform: str, url: str) -> str:
    parsed = urlparse(html.unescape(url))
    if platform == "tiktok":
        knowledge_id = parse_qs(parsed.query).get("knowledge_id", [""])[0]
        return knowledge_id or urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    path = re.sub(r"^/(global/)?", "/", parsed.path).rstrip("/") or "/"
    path = re.sub(r"^/(en|zh|ru|tr)/", "/", path)
    return path.lower()


def normalize_tiktok_url(href: str) -> str | None:
    value = html.unescape(href).replace("\\u0026", "&")
    absolute = urljoin("https://seller-us.tiktok.com/", value)
    parsed = urlparse(absolute)
    if parsed.netloc != "seller-us.tiktok.com":
        return None
    query = parse_qs(parsed.query)
    knowledge_id = query.get("knowledge_id", [""])[0]
    if not re.fullmatch(r"\d{6,}", knowledge_id):
        match = re.search(r"knowledge_id(?:=|%3D)(\d{6,})", value, flags=re.I)
        if not match:
            return None
        knowledge_id = match.group(1)
    return f"https://seller-us.tiktok.com/university/essay?knowledge_id={knowledge_id}"


def normalize_ozon_url(href: str) -> str | None:
    value = html.unescape(href)
    absolute = urljoin("https://global-help.ozon.com/en/", value)
    parsed = urlparse(absolute)
    if parsed.netloc not in {"global-help.ozon.com", "docs.ozon.com"}:
        return None
    if not re.match(r"^/(en|global/en)(/|$)", parsed.path):
        return None
    path = re.sub(r"^/global/en", "/en", parsed.path).rstrip("/")
    if path in {"", "/en"}:
        return None
    return f"https://global-help.ozon.com{path}?region=CHN"


def classify(title: str, url: str) -> str:
    haystack = f"{title} {url}".lower()
    for topic, needles in TOPIC_RULES:
        if any(needle in haystack for needle in needles):
            return topic
    return "其他官方指南"


def snapshot_links(platform: str) -> Iterable[dict]:
    configured = read_config(platform)["sources"]
    configured_by_identity = {
        configured_identity(platform, source["url"]): source["source_key"]
        for source in configured
    }
    snapshot_root = ROOT / "data" / platform / "snapshots"
    if not snapshot_root.exists():
        return []
    rows: dict[str, dict] = {}
    for path in sorted(snapshot_root.rglob("*.html")):
        parser = AnchorParser()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        for link in parser.links:
            url = normalize_tiktok_url(link.href) if platform == "tiktok" else normalize_ozon_url(link.href)
            if not url:
                continue
            identity = configured_identity(platform, url)
            title = link.text.strip() or identity
            existing = rows.get(identity)
            candidate = {
                "platform": platform,
                "title": title,
                "url": url,
                "topic": classify(title, url),
                "configured": identity in configured_by_identity,
                "configured_source_key": configured_by_identity.get(identity),
                "discovered_from_snapshot": path.relative_to(ROOT).as_posix(),
                "discovery_method": "official_page_internal_link",
            }
            if existing is None or len(title) > len(existing["title"]):
                rows[identity] = candidate
    return rows.values()


def markdown_report(payload: dict) -> str:
    lines = [
        "# 官方来源站内发现清单",
        "",
        f"- 生成时间（UTC）：{payload['generated_at']}",
        "- 发现方法：仅解析项目内已核验官方页面快照中的站内链接；搜索摘要未作为证据。",
        "- `configured=false` 表示官方页面可发现但当前配置未纳入，不等于页面内容已完成规则级核验。",
        "",
        "## 汇总",
        "",
        "| 平台 | 发现链接 | 已配置 | 待评估补充 |",
        "|---|---:|---:|---:|",
    ]
    for platform, stats in payload["summary"]["platforms"].items():
        lines.append(
            f"| {platform} | {stats['discovered']} | {stats['configured']} | {stats['unconfigured']} |"
        )
    lines.extend(["", "## 按主题统计", "", "| 平台 | 主题 | 链接数 | 待评估 |", "|---|---|---:|---:|"])
    for row in payload["summary"]["topics"]:
        lines.append(
            f"| {row['platform']} | {row['topic']} | {row['count']} | {row['unconfigured']} |"
        )
    lines.extend(["", "## 高优先级待补来源（前 120 项）", "", "| 平台 | 主题 | 官方标题 | 官方 URL |", "|---|---|---|---|"])
    priority_topics = {
        "合同与入驻",
        "禁限售与资质",
        "订单与履约",
        "退款退货",
        "费用与结算",
        "绩效处罚与申诉",
        "内容与达人",
    }
    candidates = [
        item for item in payload["sources"] if not item["configured"] and item["topic"] in priority_topics
    ][:120]
    for item in candidates:
        title = item["title"].replace("|", "\\|")
        lines.append(f"| {item['platform']} | {item['topic']} | {title} | {item['url']} |")
    lines.extend(
        [
            "",
            "## 解释限制",
            "",
            "- 本清单证明“官方站内存在可发现入口”，不自动证明其适用于所有市场、主体或履约方式。",
            "- 正式入库前仍需抓取正文、确认页面不是动态空壳，并记录范围、版本日期与核验时间。",
            "- 合同、具体规则、通用政策、操作指南、新闻公告应按证据优先级分别处理。",
            "",
        ]
    )
    return "\n".join(lines)


def build_payload() -> dict:
    sources = sorted(
        [*snapshot_links("tiktok"), *snapshot_links("ozon")],
        key=lambda item: (item["platform"], item["topic"], item["title"].lower(), item["url"]),
    )
    platform_stats: dict[str, dict[str, int]] = {}
    topic_rows: list[dict] = []
    for platform in ("tiktok", "ozon"):
        selected = [item for item in sources if item["platform"] == platform]
        platform_stats[platform] = {
            "discovered": len(selected),
            "configured": sum(1 for item in selected if item["configured"]),
            "unconfigured": sum(1 for item in selected if not item["configured"]),
        }
        counts = Counter(item["topic"] for item in selected)
        unconfigured = Counter(item["topic"] for item in selected if not item["configured"])
        for topic in sorted(counts):
            topic_rows.append(
                {
                    "platform": platform,
                    "topic": topic,
                    "count": counts[topic],
                    "unconfigured": unconfigured[topic],
                }
            )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evidence_policy": "official_internal_links_only",
        "summary": {"platforms": platform_stats, "topics": topic_rows},
        "sources": sources,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--date", default=DEFAULT_DATE)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    store = ProfileStore(ROOT)
    profile_id = args.profile or store.active()
    if not profile_id:
        print(json.dumps({"ok": False, "error": "尚无平台档案；请先运行 cli.py onboard"}, ensure_ascii=False, indent=2))
        return 2
    payload = RuleService(ROOT, profile_id).discover(
        timeout=args.timeout, max_pages=args.max_pages
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"official-source-discovery-{profile_id}-{args.date}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": payload.get("ok", False),
                "json": str(json_path),
                "profile_id": profile_id,
                "visited": payload.get("visited", 0),
                "source_count": payload.get("source_count", 0),
                "pending_review": sum(
                    item.get("status") == "pending"
                    for item in payload.get("candidates", [])
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
