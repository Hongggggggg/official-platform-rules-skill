from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from collections import deque
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .fetch import fetch_url


REQUIRED_TOPICS = (
    "合同与入驻",
    "禁限售",
    "类目资质",
    "商品上架",
    "知识产权",
    "内容与广告",
    "订单履约",
    "退款售后",
    "费用结算",
    "绩效处罚",
    "申诉",
    "数据隐私",
)

TOPIC_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("合同与入驻", ("contract", "terms", "agreement", "register", "onboarding", "入驻", "合同", "条款")),
    ("禁限售", ("prohibited", "restricted", "banned", "禁售", "限售")),
    ("类目资质", ("qualification", "certificate", "category approval", "类目", "资质", "认证")),
    ("商品上架", ("listing", "product detail", "catalog", "sku", "上架", "商品信息")),
    ("知识产权", ("intellectual property", "copyright", "trademark", "counterfeit", "知识产权", "假货")),
    ("内容与广告", ("advertising", "ads policy", "creator", "affiliate", "content policy", "广告", "内容", "达人")),
    ("订单履约", ("fulfillment", "shipping", "delivery", "packaging", "order", "履约", "发货", "物流", "订单")),
    ("退款售后", ("return", "refund", "replacement", "exchange", "退款", "退货", "售后")),
    ("费用结算", ("fee", "commission", "payment", "settlement", "费用", "佣金", "结算")),
    ("绩效处罚", ("enforcement", "violation", "suspension", "account health", "performance", "处罚", "违规", "绩效")),
    ("申诉", ("appeal", "dispute", "申诉", "复议")),
    ("数据隐私", ("privacy", "data protection", "personal data", "隐私", "数据保护")),
)

TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "ref", "source"}
ASSET_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|css|js|woff2?|ttf|zip|mp4|mp3)(?:$|\?)", re.I)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []
        self._title_depth = 0
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._title_depth += 1
        if tag.lower() != "a":
            return
        self._href = dict(attrs).get("href")
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_parts.append(data)
        if self._href:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._title_depth = max(0, self._title_depth - 1)
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, re.sub(r"\s+", " ", " ".join(self._parts)).strip()))
            self._href = None
            self._parts = []

    @property
    def title(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._title_parts)).strip()


def canonical_url(value: str, base: str | None = None) -> str | None:
    absolute = urljoin(base or value, html.unescape(value).strip())
    parsed = urlparse(absolute)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password or ASSET_RE.search(absolute):
        return None
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in TRACKING_KEYS]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunparse(("https", parsed.hostname.lower() + ((":" + str(parsed.port)) if parsed.port else ""), path.rstrip("/") or "/", "", urlencode(query), ""))


def classify(title: str, url: str) -> tuple[str, str, str]:
    haystack = f"{title} {url}".lower()
    for topic, needles in TOPIC_RULES:
        if any(needle in haystack for needle in needles):
            high = topic in {"合同与入驻", "禁限售", "类目资质", "订单履约", "退款售后", "费用结算", "绩效处罚", "申诉"}
            source_type = "contract" if topic == "合同与入驻" else "policy"
            return topic, "high" if high else "normal", source_type
    return "其他官方指南", "normal", "guide"


def source_record(url: str, title: str, topic: str, risk: str, source_type: str) -> dict[str, Any]:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    path_slug = re.sub(r"[^a-z0-9]+", "-", urlparse(url).path.lower()).strip("-")[-42:]
    source_key = f"{path_slug or 'official'}-{digest}"[:64]
    return {
        "source_key": source_key,
        "canonical_rule_key": f"discovered.{digest}",
        "url": url,
        "source_type": source_type,
        "topic": topic,
        "risk": risk,
    }


def _xml_locations(body: bytes) -> list[str]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    return [str(node.text).strip() for node in root.iter() if node.tag.lower().endswith("loc") and node.text]


def discover(
    seed_urls: list[str],
    verified_domains: set[str],
    *,
    timeout: int = 30,
    max_pages: int = 1000,
) -> dict[str, Any]:
    queue: deque[tuple[str, str, int]] = deque()
    for seed in seed_urls:
        normalized = canonical_url(seed)
        if normalized:
            queue.append((normalized, "verified_seed", 0))
            parsed = urlparse(normalized)
            queue.append((f"https://{parsed.hostname}/robots.txt", "robots", 0))
            queue.append((f"https://{parsed.hostname}/sitemap.xml", "sitemap", 0))
    visited: set[str] = set()
    candidates: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    while queue and len(visited) < max_pages:
        url, provenance, depth = queue.popleft()
        normalized = canonical_url(url)
        if not normalized or normalized in visited:
            continue
        visited.add(normalized)
        domain = urlparse(normalized).hostname or ""
        accepted_domain = domain in verified_domains
        if not accepted_domain:
            topic, risk, source_type = classify("", normalized)
            candidates[normalized] = {
                "url": normalized, "canonical_url": normalized, "domain": domain,
                "title": "", "topic": topic, "risk": risk, "source_type": source_type,
                "provenance": provenance, "status": "pending", "reason": "new_domain_requires_verification",
            }
            continue
        try:
            fetched = fetch_url(normalized, timeout=timeout, attempts=2)
        except Exception as exc:
            item = {"url": normalized, "error": f"{type(exc).__name__}: {exc}"}
            if provenance in {"robots", "sitemap", "official_robots"}:
                warnings.append(item)
            else:
                errors.append(item)
            continue
        content_lower = fetched.content_type.lower()
        body_start = fetched.body[:300].lstrip().lower()
        if "xml" in content_lower or body_start.startswith(b"<?xml") or b"<urlset" in body_start or b"<sitemapindex" in body_start:
            for location in _xml_locations(fetched.body):
                candidate = canonical_url(location, normalized)
                if candidate and candidate not in visited:
                    queue.append((candidate, "official_sitemap", depth + 1))
            continue
        if normalized.endswith("/robots.txt") or content_lower == "text/plain":
            text = fetched.body.decode("utf-8", errors="replace")
            for match in re.finditer(r"^\s*Sitemap:\s*(https://\S+)", text, re.I | re.M):
                queue.append((match.group(1), "official_robots", depth + 1))
            continue
        if "pdf" in content_lower or normalized.lower().endswith(".pdf"):
            topic, risk, source_type = classify("Official PDF", normalized)
            candidates[normalized] = {
                "url": normalized, "canonical_url": normalized,
                "domain": domain, "title": "Official PDF", "topic": topic,
                "risk": risk, "source_type": source_type,
                "provenance": provenance, "status": "accepted",
                "reason": "pdf_requires_supported_extractor_or_official_text_export",
            }
            continue
        if "html" not in content_lower:
            continue
        parser = LinkParser()
        parser.feed(fetched.body.decode("utf-8", errors="replace"))
        page_title = parser.title or next((text for _, text in parser.links if text), "")
        topic, risk, source_type = classify(page_title, normalized)
        is_seed = provenance == "verified_seed"
        if topic != "其他官方指南" or is_seed or depth > 0:
            candidates[normalized] = {
                "url": normalized, "canonical_url": normalized, "domain": domain,
                "title": page_title[:500], "topic": topic, "risk": risk,
                "source_type": source_type, "provenance": provenance,
                "status": "accepted", "reason": None,
            }
        for href, text in parser.links:
            candidate = canonical_url(href, fetched.url)
            if not candidate or candidate in visited:
                continue
            target_domain = urlparse(candidate).hostname or ""
            link_topic, _, _ = classify(text, candidate)
            if target_domain in verified_domains:
                if depth < 4 or link_topic != "其他官方指南":
                    queue.append((candidate, "official_internal_link", depth + 1))
            elif link_topic != "其他官方指南":
                queue.append((candidate, "cross_domain_official_link", depth + 1))
    accepted = [item for item in candidates.values() if item["status"] == "accepted"]
    sources = [source_record(item["url"], item["title"], item["topic"], item["risk"], item["source_type"]) for item in accepted]
    return {
        "visited": len(visited),
        "truncated": bool(queue),
        "candidates": sorted(candidates.values(), key=lambda item: item["url"]),
        "sources": sorted(sources, key=lambda item: item["source_key"]),
        "errors": errors,
        "warnings": warnings,
    }
