from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import ExtractedDocument, SourceDefinition, utc_now
from .schema_v2 import (
    PARSER_VERSION,
    SCHEMA_VERSION,
    SCOPE_FIELDS,
    migrate_v2,
    normalize_search_text,
    upsert_scope,
)
from .sources import content_hash


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_key TEXT PRIMARY KEY,
    canonical_rule_key TEXT NOT NULL,
    url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    topic TEXT NOT NULL,
    risk TEXT NOT NULL,
    market TEXT NOT NULL,
    seller_type TEXT NOT NULL,
    fulfillment TEXT NOT NULL,
    account_scoped INTEGER NOT NULL DEFAULT 0,
    last_verified_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    language TEXT NOT NULL,
    published_at TEXT,
    effective_at TEXT,
    snapshot_path TEXT NOT NULL,
    http_status INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_key TEXT NOT NULL,
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'current', 'pending', 'superseded', 'withdrawn', 'review_required'
        )
    ),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    market TEXT NOT NULL,
    seller_type TEXT NOT NULL,
    fulfillment TEXT NOT NULL,
    topic TEXT NOT NULL,
    risk TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    published_at TEXT,
    effective_at TEXT,
    verified_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES rule_versions(id),
    account_scoped INTEGER NOT NULL DEFAULT 0,
    review_reason TEXT,
    UNIQUE(rule_key, version, market, seller_type, fulfillment)
);

CREATE INDEX IF NOT EXISTS idx_rules_current
ON rule_versions(status, market, seller_type, fulfillment);

CREATE INDEX IF NOT EXISTS idx_rules_key
ON rule_versions(rule_key, version DESC);

CREATE INDEX IF NOT EXISTS idx_rules_source
ON rule_versions(source_key, status);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    result_json TEXT,
    ok INTEGER
);
"""


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _section_slug(title: str) -> str:
    latin = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if latin:
        return latin[:80]
    return content_hash(title)[:16]


def _is_future(value: str | None) -> bool:
    if not value:
        return False
    try:
        return date.fromisoformat(value) > datetime.now(timezone.utc).date()
    except ValueError:
        return False


def _tokens(question: str) -> list[str]:
    result: list[str] = []
    for token in re.findall(
        r"[a-zA-Z0-9_]{2,}|[\u0400-\u04ff]{2,}|[\u3400-\u9fff]+",
        question.lower(),
    ):
        result.append(token)
        if re.fullmatch(r"[\u3400-\u9fff]+", token) and len(token) > 2:
            result.extend(
                token[index : index + 2]
                for index in range(len(token) - 1)
            )
    stopwords = {
        "ozon", "tiktok", "shop", "seller", "official", "platform",
        "rule", "rules", "policy", "help", "美区", "美国", "当前",
        "目前", "平台", "官方", "规则", "政策", "卖家", "店铺", "商品",
        "产品", "是否", "需要", "什么", "哪些", "怎么", "如何", "多少",
        "多久", "可以", "能否", "要求", "有没有", "请问", "一下",
        "the", "and", "for", "what", "when", "where", "which", "who",
        "how", "does", "must", "with", "from", "into", "that", "this",
        "are", "was", "were", "have", "has", "use", "using", "can",
        "could", "would", "should", "average", "time",
    }
    return [token for token in dict.fromkeys(result) if token not in stopwords]


# Phrase-driven bilingual routing. The mappings identify evidence locations, not
# answers: all conclusions must still come from the matched official rule text.
QUERY_CONCEPTS: tuple[dict[str, tuple[str, ...]], ...] = (
    {"phrases": ("营业执照", "企业主体", "公司主体", "注册公司", "企业注册", "注册材料", "本土企业", "新办企业", "新公司", "入驻", "下店", "开店", "business license", "corporation", "partnership"), "terms": ("business", "registration", "corporation", "partnership", "documents", "requirements"), "sources": ("seller-registration-corporation",), "titles": ("what are the requirements", "what information will need to be provided", "what documents will be requested", "verification")},
    {"phrases": ("加州65", "加州 65", "prop 65", "proposition 65", "prop65"), "terms": ("california", "proposition", "chemical", "warning", "carcinogens"), "sources": ("california-proposition-65", "product-listing-policy"), "titles": ("california proposition 65", "requirements", "product detail page")},
    {"phrases": ("二手鞋", "二手奢侈", "pre-owned", "preowned", "二手商品", "旧货"), "terms": ("pre-owned", "secondhand", "luxury", "footwear", "authentication", "certificate of authenticity", "coa"), "sources": ("pre-owned-products", "restricted-products-policy", "prohibited-products-policy"), "titles": ("pre-owned", "authentication", "authentication requirements", "secondhand")},
    {"phrases": ("商品标题", "产品标题", "listing title", "product title"), "terms": ("product", "title", "brand"), "sources": ("product-listing-policy",), "titles": ("product title",)},
    {"phrases": ("商品描述", "产品描述", "详情描述", "product description"), "terms": ("product", "description"), "sources": ("product-listing-policy",), "titles": ("product description",)},
    {"phrases": ("详情页", "商品详情", "product detail"), "terms": ("product", "detail", "page", "mandatory"), "sources": ("product-listing-policy",), "titles": ("product detail page",)},
    {"phrases": ("\u4e3b\u56fe", "\u5546\u54c1\u56fe\u7247", "\u4ea7\u54c1\u56fe\u7247", "\u56fe\u7247\u5c3a\u5bf8", "listing image"), "exclude": ("\u5546\u54c1\u56fe\u7247\u5e16", "photo posts"), "terms": ("image", "images", "pixels", "background"), "sources": ("product-listing-policy",), "titles": ("images and videos",)},
    {"phrases": ("变体", "sku", "颜色尺码", "variation"), "terms": ("variation", "variations", "sku", "size", "color"), "sources": ("product-listing-policy",), "titles": ("product variations",)},
    {"phrases": ("禁售", "禁止销售", "不能卖", "不让卖", "prohibited"), "terms": ("prohibited", "products"), "sources": ("prohibited-products-policy",), "titles": ("prohibited products",)},
    {"phrases": ("限售", "类目资质", "类目资格", "restricted", "资格中心"), "terms": ("restricted", "qualification", "category"), "sources": ("restricted-products-policy",), "titles": ("restricted products", "qualification")},
    {"phrases": ("酒类", "酒精", "香烟", "烟草", "电子烟", "alcohol", "tobacco"), "terms": ("alcohol", "tobacco", "e-cigarettes"), "sources": ("prohibited-products-policy",), "titles": ("alcohol, tobacco",)},
    {"phrases": ("枪支", "枪械", "武器", "弹药", "firearm", "weapon"), "terms": ("firearms", "ammunition", "weapons"), "sources": ("prohibited-products-policy",), "titles": ("firearms, ammunition",)},
    {"phrases": ("药品", "医疗器械", "保健品", "medicine", "medical device"), "terms": ("medicines", "medical", "devices", "supplements"), "sources": ("prohibited-products-policy", "restricted-products-policy"), "titles": ("medicines", "medical devices", "health")},
    {"phrases": ("危险品", "危险物品", "hazardous", "dangerous goods"), "terms": ("hazardous", "dangerous", "goods", "items"), "sources": ("prohibited-products-policy", "restricted-products-policy"), "titles": ("hazardous", "dangerous goods")},
    {"phrases": ("qualification level",), "terms": ("category", "level", "qualification"), "sources": ("restricted-products-policy",), "titles": ("category-level qualification",)},
    {"phrases": ("类目级", "category level"), "terms": ("category", "level", "qualification"), "sources": ("restricted-products-policy",), "titles": ("category-level qualification",)},
    {"phrases": ("商品级", "产品级", "product level"), "terms": ("product", "level", "qualification"), "sources": ("restricted-products-policy",), "titles": ("product-level qualification",)},
    {"phrases": ("定向邀约", "仅限邀请", "邀请制", "invite-only"), "terms": ("invite-only", "qualification"), "sources": ("restricted-products-policy",), "titles": ("invite-only qualification",)},
    {"phrases": ("\u68c0\u6d4b\u62a5\u544a", "test report", "compliance check"), "terms": ("test reports", "lab test report", "compliance"), "sources": ("california-proposition-65",), "titles": ("product verification and compliance checks",)},
    {"phrases": ("发货时效", "多久发货", "承运商扫描", "dispatch sla", "in transit"), "terms": ("dispatch", "sla", "in transit", "carrier", "business days"), "sources": ("fulfillment-policy",), "titles": ("service level agreements", "dispatching an order")},
    {"phrases": ("自动取消", "auto-cancel", "auto cancellation"), "terms": ("auto-cancellation", "awaiting collection", "business days"), "sources": ("fulfillment-policy",), "titles": ("auto-cancellation", "service level agreements")},
    {"phrases": ("处理时间", "备货时间", "handling time"), "terms": ("handling", "time", "business days"), "sources": ("fulfillment-policy",), "titles": ("handling time",)},
    {"phrases": ("有效追踪率", "有效物流单号", "vtr"), "terms": ("valid tracking rate", "vtr", "tracking"), "sources": ("fulfillment-policy",), "titles": ("valid tracking rate",)},
    {"phrases": ("延迟发货率", "晚发率", "ldr"), "terms": ("late dispatch rate", "ldr", "dispatch"), "sources": ("fulfillment-policy",), "titles": ("late dispatch rate",)},
    {"phrases": ("按时送达率", "准时送达率", "otdr"), "terms": ("on-time delivery rate", "otdr", "delivered"), "sources": ("fulfillment-policy",), "titles": ("on-time delivery rate",)},
    {"phrases": ("卖家责任取消率", "卖家取消率", "sfcr"), "terms": ("seller-fault cancellation rate", "sfcr", "cancelled"), "sources": ("fulfillment-policy",), "titles": ("seller-fault cancellation rate",)},
    {"phrases": ("\u53ef\u9000\u5546\u54c1", "\u5bc4\u56de", "ship a return", "seller to review"), "terms": ("returnable", "ship", "review", "business days"), "sources": ("returns-refunds-replacements",), "titles": ("returnable products",)},
    {"phrases": ("退货", "退款", "return", "refund"), "terms": ("return", "refund", "requests"), "sources": ("returns-refunds-replacements",), "titles": ("returnable products",)},
    {"phrases": ("补发", "换货", "replacement", "exchange"), "terms": ("replacement", "exchange", "business day"), "sources": ("returns-refunds-replacements",), "titles": ("handling exchanges and replacements",)},
    {"phrases": ("无退货退款", "仅退款", "refund without return"), "terms": ("refund without return", "returnless refund"), "sources": ("returns-refunds-replacements",), "titles": ("seller-preferred refund without return",)},
    {"phrases": ("达人", "创作者", "creator", "affiliate creator", "联盟达人"), "terms": ("creator", "affiliate", "followers"), "sources": ("creator-eligibility",), "titles": ("affiliate creator",)},
    {"phrases": ("官方账号", "官方达人", "official shop creator"), "terms": ("official shop creator", "bound", "followers"), "sources": ("creator-eligibility",), "titles": ("official shop creator",)},
    {"phrases": ("营销达人", "营销账号", "marketing creator"), "terms": ("marketing creator", "bound", "invited"), "sources": ("creator-eligibility",), "titles": ("marketing creator",)},
    {"phrases": ("新手期", "试用期", "pilot program", "毕业条件"), "terms": ("pilot program", "graduation", "followers", "days"), "sources": ("creator-eligibility",), "titles": ("pilot program", "graduation requirements", "program restrictions")},
    {"phrases": ("挂车视频", "带货视频", "可购物视频", "发帖上限", "posting limit"), "terms": ("posting limits", "shoppable", "videos", "photo posts"), "sources": ("creator-eligibility",), "titles": ("posting limits",)},
    {"phrases": ("\u5e26\u8d27\u77ed\u89c6\u9891", "\u5546\u54c1\u56fe\u7247\u5e16", "shoppable videos", "photo posts"), "terms": ("posting limits", "shoppable", "videos", "photo posts"), "sources": ("creator-eligibility",), "titles": ("posting limits",)},
    {"phrases": ("冻结余额", "冻结货款", "扣留余额", "withholding balance"), "terms": ("withholding", "balance", "funds"), "sources": ("seller-enforcement-policy",), "titles": ("withholding balance",)},
    {"phrases": ("关联账号", "账号关联", "connected account"), "terms": ("connected", "accounts", "related"), "sources": ("seller-enforcement-policy",), "titles": ("connected accounts",)},
    {"phrases": ("申诉", "appeal"), "terms": ("appeal", "violation", "days"), "sources": ("seller-enforcement-policy",), "titles": ("appeal",)},
    {"phrases": ("包装", "包裹尺寸", "包装尺寸", "packaging", "package dimensions", "package format"), "terms": ("packaging", "package", "упаковк", "размер"), "sources": ("packaging-update", "partner-delivery"), "titles": ("упаковк", "packaging")},
    {"phrases": ("合作物流", "合作配送", "partner delivery", "中国邮政", "china post", "物流方式"), "terms": ("partner delivery", "china post", "shipping provider", "delivery methods"), "sources": ("partner-delivery",), "titles": ("ozon partner delivery",)},
    {"phrases": ("物流标签", "面单", "条形码", "label", "barcode"), "terms": ("label", "barcode", "tape", "package"), "sources": ("partner-delivery", "packaging-update"), "titles": ("ozon partner delivery", "упаковк")},
    {"phrases": ("促销", "活动", "折扣", "promotion"), "terms": ("акции", "promotion", "discount", "скидк"), "sources": ("promotions",), "titles": ("акции", "promotion")},
    {"phrases": ("广告素材", "横幅", "banner", "落地页", "landing page", "广告文字"), "terms": ("advertising", "banner", "landing page", "text"), "sources": ("display-advertising",), "titles": ("requirements for advertising materials",)},
    {"phrases": ("商品卡", "创建商品", "商品管理", "product card", "working with products"), "terms": ("working", "products", "product card"), "sources": ("product-management",), "titles": ("working with products",)},
)


def _query_profile(question: str) -> dict[str, Any]:
    lowered = question.lower()
    raw_terms = set(_tokens(question))
    weighted_terms: dict[str, int] = {token: 1 for token in raw_terms}
    source_hints: set[str] = set()
    title_hints: set[str] = set()
    matched_concepts: list[str] = []
    # Questions frequently quote an exact official section title. Treat the
    # quoted phrase as a title hint, including Cyrillic Ozon headings, while
    # still requiring a match against stored official evidence.
    quoted_titles = {
        re.sub(r"\s+", " ", value).strip().lower()
        for value in re.findall(r"[“\"「『](.{3,180}?)[”\"」』]", question)
    }
    title_hints.update(value for value in quoted_titles if value)
    for value in quoted_titles:
        for token in _tokens(value):
            weighted_terms[token] = max(weighted_terms.get(token, 0), 4)
    for concept in (*QUERY_CONCEPTS, *_configured_query_concepts()):
        phrases = concept["phrases"]
        if any(phrase.lower() in lowered for phrase in concept.get("exclude", ())):
            continue
        matched = next((phrase for phrase in phrases if phrase.lower() in lowered), None)
        if not matched:
            continue
        matched_concepts.append(matched)
        for term in concept.get("terms", ()):
            weighted_terms[term.lower()] = max(weighted_terms.get(term.lower(), 0), 3)
        source_hints.update(concept.get("sources", ()))
        title_hints.update(value.lower() for value in concept.get("titles", ()))
    if "posting limits" in title_hints:
        title_hints.discard("affiliate creator")
    return {
        "weighted_terms": weighted_terms,
        "raw_terms": raw_terms,
        "source_hints": source_hints,
        "title_hints": title_hints,
        "matched_concepts": matched_concepts,
    }

SOURCE_PRIORITY = {
    "contract": 5,
    "specific_rule": 4,
    "policy": 3,
    "guide": 2,
    "news": 1,
}


@lru_cache(maxsize=1)
def _configured_query_concepts() -> tuple[dict[str, tuple[str, ...]], ...]:
    path = Path(__file__).resolve().parents[2] / "config" / "query-concepts.json"
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return ()
    if payload.get("schema_version") != SCHEMA_VERSION:
        return ()
    concepts: list[dict[str, tuple[str, ...]]] = []
    for item in payload.get("concepts", []):
        if not isinstance(item, dict) or not item.get("phrases"):
            continue
        concepts.append(
            {
                key: tuple(str(value) for value in item.get(key, ()))
                for key in ("phrases", "exclude", "terms", "sources", "titles")
                if item.get(key)
            }
        )
    return tuple(concepts)


def _scope_parts(value: str) -> set[str]:
    return {
        item
        for item in re.split(r"[^a-z0-9\u0400-\u04ff\u3400-\u9fff]+", value.lower())
        if item and item not in {"or", "and"}
    }


def _scope_value_matches(stored: str | None, requested: str) -> bool:
    current = (stored or "all").strip().lower()
    wanted = requested.strip().lower()
    if current in {"all", "any", "*"} or not wanted:
        return True
    if current == wanted:
        return True
    current_parts = _scope_parts(current)
    wanted_parts = _scope_parts(wanted)
    return bool(wanted_parts) and wanted_parts.issubset(current_parts)


def _row_matches_scope(row: sqlite3.Row, scope: dict[str, str]) -> bool:
    keys = set(row.keys())
    qualifiers: dict[str, str] = {}
    if "scope_qualifiers_json" in keys and row["scope_qualifiers_json"]:
        try:
            qualifiers = json.loads(str(row["scope_qualifiers_json"]))
        except json.JSONDecodeError:
            qualifiers = {}
    for field, requested in scope.items():
        alias = f"scope_{field}"
        if alias in keys:
            stored = row[alias]
        elif field in keys:
            stored = row[field]
        else:
            stored = qualifiers.get(field, "all")
        if not _scope_value_matches(
            str(stored) if stored is not None else None,
            requested,
        ):
            return False
    return True


class RuleDatabase:
    def __init__(self, path: Path, platform: str = "test") -> None:
        self.path = path
        self.platform = platform

    def initialize(self) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.path) as connection:
            connection.executescript(SCHEMA)
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(rule_versions)")
            }
            if "review_reason" not in columns:
                connection.execute(
                    "ALTER TABLE rule_versions ADD COLUMN review_reason TEXT"
                )
            try:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS rules_fts
                    USING fts5(rule_id UNINDEXED, title, content, topic, tokenize='unicode61')
                    """
                )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('fts5', 'true')"
                )
            except sqlite3.OperationalError:
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('fts5', 'false')"
                )
            result = migrate_v2(connection, self.platform)
            result["quality_cleanup"] = self._quarantine_known_noise(connection)
            return result

    def _quarantine_known_noise(
        self, connection: sqlite3.Connection
    ) -> dict[str, int]:
        bad_titles = (
            "похоже, нет соединения",
            "просмотр статей",
            "article viewer",
            "no internet connection",
        )
        bad_title_set = {value.casefold() for value in bad_titles}
        bad_rows = [
            row
            for row in connection.execute(
                """
                SELECT * FROM rule_versions
                WHERE status='current'
                ORDER BY id
                """
            ).fetchall()
            if str(row["title"]).strip().casefold() in bad_title_set
        ]
        if not bad_rows:
            return {"quarantined": 0, "restored": 0}
        affected_sources = {str(row["source_key"]) for row in bad_rows}
        today = datetime.now(timezone.utc).date().isoformat()
        now = utc_now()
        for row in bad_rows:
            connection.execute(
                """
                UPDATE rule_versions
                SET status='withdrawn', review_reason='quality_gate_v2',
                    valid_to=COALESCE(valid_to, ?)
                WHERE id=?
                """,
                (today, row["id"]),
            )
            connection.execute(
                """
                UPDATE effective_intervals
                SET valid_to=COALESCE(valid_to, ?), retired_at=?
                WHERE rule_version_id=?
                """,
                (today, now, row["id"]),
            )
            self._remove_fts(connection, int(row["id"]))
        restored = 0
        for source_key in affected_sources:
            candidates = connection.execute(
                """
                SELECT * FROM rule_versions
                WHERE source_key=? AND status='review_required'
                  AND review_reason='section_missing'
                ORDER BY version DESC, id DESC
                """,
                (source_key,),
            ).fetchall()
            for row in candidates:
                competing = connection.execute(
                    """
                    SELECT id FROM rule_versions
                    WHERE rule_key=? AND scope_id=? AND status='current'
                    LIMIT 1
                    """,
                    (row["rule_key"], row["scope_id"]),
                ).fetchone()
                if competing:
                    continue
                connection.execute(
                    """
                    UPDATE rule_versions
                    SET status='current', review_reason=NULL, valid_to=NULL
                    WHERE id=?
                    """,
                    (row["id"],),
                )
                connection.execute(
                    """
                    UPDATE effective_intervals
                    SET valid_to=NULL, retired_at=NULL
                    WHERE rule_version_id=?
                    """,
                    (row["id"],),
                )
                self._add_fts(
                    connection,
                    int(row["id"]),
                    str(row["title"]),
                    str(row["content"]),
                    str(row["topic"]),
                )
                restored += 1
            connection.execute(
                """
                UPDATE sources
                SET last_error='V2质量门禁隔离动态空壳；需重新核验',
                    updated_at=?
                WHERE source_key=?
                """,
                (now, source_key),
            )
        self._bump_revision(connection)
        return {"quarantined": len(bad_rows), "restored": restored}

    @staticmethod
    def _revision(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='database_revision'"
        ).fetchone()
        return int(row["value"]) if row else 0

    @classmethod
    def _bump_revision(cls, connection: sqlite3.Connection) -> int:
        revision = cls._revision(connection) + 1
        connection.execute(
            """
            INSERT OR REPLACE INTO metadata(key, value)
            VALUES('database_revision', ?)
            """,
            (str(revision),),
        )
        return revision

    def database_revision(self) -> int:
        with _connect(self.path) as connection:
            return self._revision(connection)

    def start_sync(
        self, mode: str, source_keys: Iterable[str] | None = None
    ) -> int:
        with _connect(self.path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO sync_runs(
                    started_at, mode, schema_version, source_keys_json
                ) VALUES(?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    mode,
                    SCHEMA_VERSION,
                    json.dumps(sorted(source_keys or ()), ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def finish_sync(self, run_id: int, ok: bool, result: dict[str, Any]) -> None:
        with _connect(self.path) as connection:
            revision = self._revision(connection)
            connection.execute(
                """
                UPDATE sync_runs
                SET finished_at = ?, ok = ?, result_json = ?,
                    database_revision = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    int(ok),
                    json.dumps(result, ensure_ascii=False),
                    revision,
                    run_id,
                ),
            )

    def upsert_source(self, source: SourceDefinition) -> None:
        now = utc_now()
        with _connect(self.path) as connection:
            scope_id = upsert_scope(
                connection,
                self.platform,
                source.scope,
                source.account_scoped,
            )
            connection.execute(
                """
                INSERT INTO sources(
                    source_key, canonical_rule_key, url, source_type, topic, risk,
                    market, seller_type, fulfillment, account_scoped, scope_id,
                    updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    canonical_rule_key=excluded.canonical_rule_key,
                    url=excluded.url,
                    source_type=excluded.source_type,
                    topic=excluded.topic,
                    risk=excluded.risk,
                    market=excluded.market,
                    seller_type=excluded.seller_type,
                    fulfillment=excluded.fulfillment,
                    account_scoped=excluded.account_scoped,
                    scope_id=excluded.scope_id,
                    updated_at=excluded.updated_at
                """,
                (
                    source.source_key,
                    source.canonical_rule_key,
                    source.url,
                    source.source_type,
                    source.topic,
                    source.risk,
                    source.scope.get("market", "all"),
                    source.scope.get("seller_type", "all"),
                    source.scope.get("fulfillment", "all"),
                    int(source.account_scoped),
                    scope_id,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE rule_versions
                SET market=?, seller_type=?, fulfillment=?, topic=?, risk=?,
                    source_type=?, source_url=?, account_scoped=?, scope_id=?
                WHERE source_key=?
                """,
                (
                    source.scope.get("market", "all"),
                    source.scope.get("seller_type", "all"),
                    source.scope.get("fulfillment", "all"),
                    source.topic,
                    source.risk,
                    source.source_type,
                    source.url,
                    int(source.account_scoped),
                    scope_id,
                    source.source_key,
                ),
            )

    def source_cache_headers(self, source_key: str) -> dict[str, str | None]:
        with _connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT etag, last_modified FROM sources WHERE source_key=?
                """,
                (source_key,),
            ).fetchone()
        return {
            "etag": str(row["etag"]) if row and row["etag"] else None,
            "last_modified": (
                str(row["last_modified"])
                if row and row["last_modified"]
                else None
            ),
        }

    def start_fetch(self, source_key: str, sync_run_id: int | None) -> int:
        with _connect(self.path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO fetch_runs(sync_run_id, source_key, started_at)
                VALUES(?, ?, ?)
                """,
                (sync_run_id, source_key, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_fetch(
        self,
        fetch_run_id: int,
        *,
        outcome: str,
        http_status: int | None = None,
        final_url: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        page_hash: str | None = None,
        snapshot_id: int | None = None,
        error: str | None = None,
    ) -> None:
        with _connect(self.path) as connection:
            connection.execute(
                """
                UPDATE fetch_runs
                SET finished_at=?, outcome=?, http_status=?, final_url=?,
                    etag=?, last_modified=?, content_hash=?, snapshot_id=?,
                    error=?
                WHERE id=?
                """,
                (
                    utc_now(),
                    outcome,
                    http_status,
                    final_url,
                    etag,
                    last_modified,
                    page_hash,
                    snapshot_id,
                    error[:2000] if error else None,
                    fetch_run_id,
                ),
            )
            connection.execute(
                """
                UPDATE sources
                SET etag=COALESCE(?, etag),
                    last_modified=COALESCE(?, last_modified),
                    last_http_status=COALESCE(?, last_http_status),
                    last_fetch_run_id=?,
                    updated_at=?
                WHERE source_key=(
                    SELECT source_key FROM fetch_runs WHERE id=?
                )
                """,
                (
                    etag,
                    last_modified,
                    http_status,
                    fetch_run_id,
                    utc_now(),
                    fetch_run_id,
                ),
            )

    def record_not_modified(
        self,
        source_key: str,
        fetched_at: str,
        fetch_run_id: int,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> dict[str, Any]:
        with _connect(self.path) as connection:
            connection.execute(
                """
                UPDATE sources
                SET last_verified_at=?, last_error=NULL,
                    etag=COALESCE(?, etag),
                    last_modified=COALESCE(?, last_modified),
                    last_http_status=304,
                    last_fetch_run_id=?,
                    updated_at=?
                WHERE source_key=?
                """,
                (
                    fetched_at,
                    etag,
                    last_modified,
                    fetch_run_id,
                    utc_now(),
                    source_key,
                ),
            )
            connection.execute(
                """
                UPDATE rule_versions
                SET verified_at=?
                WHERE source_key=?
                  AND status IN ('current', 'pending', 'review_required')
                """,
                (fetched_at, source_key),
            )
        return {
            "source_key": source_key,
            "status": "not_modified",
            "rules_created": 0,
        }

    def record_source_error(self, source_key: str, message: str) -> None:
        with _connect(self.path) as connection:
            connection.execute(
                "UPDATE sources SET last_error=?, updated_at=? WHERE source_key=?",
                (message[:2000], utc_now(), source_key),
            )

    def latest_snapshot_hash(self, source_key: str) -> str | None:
        with _connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT content_hash FROM snapshots
                WHERE source_key=?
                ORDER BY id DESC LIMIT 1
                """,
                (source_key,),
            ).fetchone()
        return str(row["content_hash"]) if row else None

    def ingest(
        self,
        source: SourceDefinition,
        document: ExtractedDocument,
        snapshot_path: str,
        http_status: int,
        fetched_at: str,
        fetch_run_id: int | None = None,
        raw_body: bytes | None = None,
    ) -> dict[str, Any]:
        page_hash = content_hash(document.text)
        raw_hash = content_hash(raw_body) if raw_body is not None else page_hash
        now = utc_now()
        with _connect(self.path) as connection:
            scope_id = upsert_scope(
                connection,
                self.platform,
                source.scope,
                source.account_scoped,
            )
            prior_snapshot = connection.execute(
                """
                SELECT id, content_hash FROM snapshots
                WHERE source_key=? ORDER BY id DESC LIMIT 1
                """,
                (source.source_key,),
            ).fetchone()
            if prior_snapshot and prior_snapshot["content_hash"] == page_hash:
                connection.execute(
                    """
                    UPDATE sources
                    SET last_verified_at=?, last_error=NULL, updated_at=?
                    WHERE source_key=?
                    """,
                    (fetched_at, now, source.source_key),
                )
                connection.execute(
                    """
                    UPDATE rule_versions
                    SET verified_at=?,
                        published_at=COALESCE(published_at, ?),
                        effective_at=COALESCE(effective_at, ?)
                    WHERE source_key=? AND status IN ('current', 'pending', 'review_required')
                    """,
                    (fetched_at, document.published_at, document.effective_at, source.source_key),
                )
                return {
                    "source_key": source.source_key,
                    "status": "unchanged",
                    "rules_created": 0,
                    "content_hash": page_hash,
                    "snapshot_id": int(prior_snapshot["id"]),
                }

            snapshot_cursor = connection.execute(
                """
                INSERT INTO snapshots(
                    source_key, fetched_at, content_hash, title, language,
                    published_at, effective_at, snapshot_path, http_status,
                    raw_content_hash, raw_bytes, fetch_run_id, parser_version
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.source_key,
                    fetched_at,
                    page_hash,
                    document.title,
                    document.language,
                    document.published_at,
                    document.effective_at,
                    snapshot_path,
                    http_status,
                    raw_hash,
                    len(raw_body) if raw_body is not None else 0,
                    fetch_run_id,
                    PARSER_VERSION,
                ),
            )
            snapshot_id = int(snapshot_cursor.lastrowid)

            created: list[dict[str, Any]] = []
            seen_keys: set[str] = set()
            for ordinal, (heading, body) in enumerate(document.sections):
                normalized_body = body.strip()
                if not normalized_body:
                    continue
                rule_key = (
                    f"{source.canonical_rule_key}::{_section_slug(heading)}"
                )
                seen_keys.add(rule_key)
                section_hash = content_hash(normalized_body)
                section_cursor = connection.execute(
                    """
                    INSERT INTO extracted_sections(
                        snapshot_id, section_key, heading, content, content_hash,
                        ordinal, parser_version, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        rule_key,
                        heading[:500],
                        normalized_body,
                        section_hash,
                        ordinal,
                        PARSER_VERSION,
                        now,
                    ),
                )
                section_id = int(section_cursor.lastrowid)
                prior = connection.execute(
                    """
                    SELECT * FROM rule_versions
                    WHERE rule_key=? AND market=? AND seller_type=? AND fulfillment=?
                    ORDER BY version DESC LIMIT 1
                    """,
                    (
                        rule_key,
                        source.scope.get("market", "all"),
                        source.scope.get("seller_type", "all"),
                        source.scope.get("fulfillment", "all"),
                    ),
                ).fetchone()
                if prior and prior["content_hash"] == section_hash:
                    if (
                        prior["status"] == "review_required"
                        and prior["review_reason"] == "section_missing"
                    ):
                        competing = connection.execute(
                            """
                            SELECT id FROM rule_versions
                            WHERE rule_key=? AND market=? AND seller_type=?
                              AND fulfillment=? AND status='current' AND id<>?
                            LIMIT 1
                            """,
                            (
                                rule_key,
                                source.scope.get("market", "all"),
                                source.scope.get("seller_type", "all"),
                                source.scope.get("fulfillment", "all"),
                                prior["id"],
                            ),
                        ).fetchone()
                        if not competing:
                            connection.execute(
                                """
                                UPDATE rule_versions
                                SET status='current', review_reason=NULL,
                                    verified_at=?, section_id=?, scope_id=?
                                WHERE id=?
                                """,
                                (
                                    fetched_at,
                                    section_id,
                                    scope_id,
                                    prior["id"],
                                ),
                            )
                            self._add_fts(
                                connection,
                                int(prior["id"]),
                                str(prior["title"]),
                                str(prior["content"]),
                                str(prior["topic"]),
                            )
                            connection.execute(
                                """
                                INSERT OR IGNORE INTO evidence_links(
                                    rule_version_id, snapshot_id, section_id,
                                    source_url, fragment, content_hash, linked_at
                                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    prior["id"],
                                    snapshot_id,
                                    section_id,
                                    source.url,
                                    heading[:500],
                                    section_hash,
                                    fetched_at,
                                ),
                            )
                            continue
                    connection.execute(
                        """
                        UPDATE rule_versions
                        SET verified_at=?, section_id=?, scope_id=?
                        WHERE id=?
                        """,
                        (fetched_at, section_id, scope_id, prior["id"]),
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO evidence_links(
                            rule_version_id, snapshot_id, section_id,
                            source_url, fragment, content_hash, linked_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            prior["id"],
                            snapshot_id,
                            section_id,
                            source.url,
                            heading[:500],
                            section_hash,
                            fetched_at,
                        ),
                    )
                    continue

                version = int(prior["version"]) + 1 if prior else 1
                current = connection.execute(
                    """
                    SELECT * FROM rule_versions
                    WHERE rule_key=? AND market=? AND seller_type=? AND fulfillment=?
                      AND status='current'
                    ORDER BY version DESC LIMIT 1
                    """,
                    (
                        rule_key,
                        source.scope.get("market", "all"),
                        source.scope.get("seller_type", "all"),
                        source.scope.get("fulfillment", "all"),
                    ),
                ).fetchone()

                review_reason: str | None = None
                if _is_future(document.effective_at):
                    status = "pending"
                elif (
                    current
                    and SOURCE_PRIORITY[source.source_type]
                    < SOURCE_PRIORITY[str(current["source_type"])]
                ):
                    status = "review_required"
                    review_reason = "lower_priority_conflict"
                elif (
                    current
                    and not document.published_at
                    and not document.effective_at
                ):
                    status = "review_required"
                    review_reason = "undated_change"
                else:
                    status = "current"

                supersedes_id: int | None = None
                valid_from = (
                    document.effective_at
                    or document.published_at
                    or fetched_at[:10]
                )
                if status == "current" and current:
                    supersedes_id = int(current["id"])
                    connection.execute(
                        """
                        UPDATE rule_versions
                        SET status='superseded', valid_to=?
                        WHERE id=?
                        """,
                        (valid_from, supersedes_id),
                    )
                    connection.execute(
                        """
                        UPDATE effective_intervals
                        SET valid_to=?, retired_at=?
                        WHERE rule_version_id=?
                        """,
                        (valid_from, fetched_at, supersedes_id),
                    )
                    self._remove_fts(connection, supersedes_id)

                cursor = connection.execute(
                    """
                    INSERT INTO rule_versions(
                        rule_key, source_key, version, status, title, content,
                        content_hash, market, seller_type, fulfillment, topic, risk,
                        source_type, source_url, published_at, effective_at,
                        verified_at, created_at, supersedes_id, account_scoped,
                        review_reason, scope_id, section_id, valid_from, valid_to,
                        observed_at
                    ) VALUES(
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        rule_key,
                        source.source_key,
                        version,
                        status,
                        heading[:500],
                        normalized_body,
                        section_hash,
                        source.scope.get("market", "all"),
                        source.scope.get("seller_type", "all"),
                        source.scope.get("fulfillment", "all"),
                        source.topic,
                        source.risk,
                        source.source_type,
                        source.url,
                        document.published_at,
                        document.effective_at,
                        fetched_at,
                        now,
                        supersedes_id,
                        int(source.account_scoped),
                        review_reason,
                        scope_id,
                        section_id,
                        valid_from,
                        None,
                        fetched_at,
                    ),
                )
                rule_id = int(cursor.lastrowid)
                if status == "current":
                    self._add_fts(
                        connection, rule_id, heading, normalized_body, source.topic
                    )
                self._add_fts_v2(
                    connection,
                    rule_id,
                    heading,
                    normalized_body,
                    source.topic,
                )
                connection.execute(
                    """
                    INSERT INTO effective_intervals(
                        rule_version_id, valid_from, valid_to, observed_at,
                        retired_at, created_at
                    ) VALUES(?, ?, NULL, ?, NULL, ?)
                    """,
                    (rule_id, valid_from, fetched_at, now),
                )
                connection.execute(
                    """
                    INSERT INTO evidence_links(
                        rule_version_id, snapshot_id, section_id, source_url,
                        fragment, content_hash, linked_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rule_id,
                        snapshot_id,
                        section_id,
                        source.url,
                        heading[:500],
                        section_hash,
                        fetched_at,
                    ),
                )
                created.append(
                    {
                        "rule_id": rule_id,
                        "rule_key": rule_key,
                        "version": version,
                        "status": status,
                    }
                )

            missing_rows = connection.execute(
                """
                SELECT * FROM rule_versions
                WHERE source_key=? AND status='current'
                """,
                (source.source_key,),
            ).fetchall()
            for missing in missing_rows:
                if str(missing["rule_key"]) in seen_keys:
                    continue
                connection.execute(
                    """
                    UPDATE rule_versions
                    SET status='review_required', review_reason='section_missing'
                    WHERE id=?
                    """,
                    (missing["id"],),
                )
                self._remove_fts(connection, int(missing["id"]))

            connection.execute(
                """
                UPDATE sources
                SET last_verified_at=?, last_error=NULL, updated_at=?
                WHERE source_key=?
                """,
                (fetched_at, now, source.source_key),
            )
            self._bump_revision(connection)
            return {
                "source_key": source.source_key,
                "status": "changed" if prior_snapshot else "new",
                "rules_created": len(created),
                "rules": created,
                "content_hash": page_hash,
                "snapshot_id": snapshot_id,
            }

    def activate_due_pending(self) -> dict[str, int]:
        today = datetime.now(timezone.utc).date().isoformat()
        activated = 0
        review_required = 0
        with _connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM rule_versions
                WHERE status='pending' AND effective_at IS NOT NULL
                  AND effective_at <= ?
                ORDER BY effective_at, version, id
                """,
                (today,),
            ).fetchall()
            for row in rows:
                current = connection.execute(
                    """
                    SELECT * FROM rule_versions
                    WHERE rule_key=? AND market=? AND seller_type=?
                      AND fulfillment=? AND status='current'
                    ORDER BY version DESC LIMIT 1
                    """,
                    (row["rule_key"], row["market"], row["seller_type"], row["fulfillment"]),
                ).fetchone()
                if (
                    current
                    and SOURCE_PRIORITY[str(row["source_type"])]
                    < SOURCE_PRIORITY[str(current["source_type"])]
                ):
                    connection.execute(
                        """
                        UPDATE rule_versions
                        SET status='review_required', review_reason='lower_priority_conflict'
                        WHERE id=?
                        """,
                        (row["id"],),
                    )
                    review_required += 1
                    continue
                supersedes_id = None
                if current:
                    supersedes_id = int(current["id"])
                    connection.execute(
                        """
                        UPDATE rule_versions
                        SET status='superseded', valid_to=?
                        WHERE id=?
                        """,
                        (row["effective_at"], supersedes_id),
                    )
                    connection.execute(
                        """
                        UPDATE effective_intervals
                        SET valid_to=?, retired_at=?
                        WHERE rule_version_id=?
                        """,
                        (row["effective_at"], utc_now(), supersedes_id),
                    )
                    self._remove_fts(connection, supersedes_id)
                connection.execute(
                    """
                    UPDATE rule_versions
                    SET status='current', supersedes_id=?, review_reason=NULL
                    WHERE id=?
                    """,
                    (supersedes_id, row["id"]),
                )
                self._add_fts(
                    connection, int(row["id"]), str(row["title"]),
                    str(row["content"]), str(row["topic"])
                )
                activated += 1
            if activated or review_required:
                self._bump_revision(connection)
        return {"activated": activated, "review_required": review_required}
    @staticmethod
    def _add_fts(
        connection: sqlite3.Connection,
        rule_id: int,
        title: str,
        content: str,
        topic: str,
    ) -> None:
        try:
            connection.execute(
                "INSERT INTO rules_fts(rule_id, title, content, topic) VALUES(?, ?, ?, ?)",
                (rule_id, title, content, topic),
            )
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _add_fts_v2(
        connection: sqlite3.Connection,
        rule_id: int,
        title: str,
        content: str,
        topic: str,
    ) -> None:
        try:
            connection.execute(
                "DELETE FROM rules_fts_v2 WHERE rule_id=?",
                (rule_id,),
            )
            connection.execute(
                """
                INSERT INTO rules_fts_v2(
                    rule_id, title, content, topic, normalized
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    title,
                    content,
                    topic,
                    normalize_search_text(f"{title} {topic} {content}"),
                ),
            )
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _remove_fts(connection: sqlite3.Connection, rule_id: int) -> None:
        try:
            connection.execute("DELETE FROM rules_fts WHERE rule_id=?", (rule_id,))
        except sqlite3.OperationalError:
            pass

    def search(
        self,
        question: str,
        limit: int = 8,
        scope: dict[str, str] | None = None,
        as_of_date: str | None = None,
    ) -> list[dict[str, Any]]:
        if as_of_date:
            try:
                date.fromisoformat(as_of_date)
            except ValueError as exc:
                raise ValueError("as_of_date 必须是 YYYY-MM-DD") from exc
        profile = _query_profile(question)
        weighted_terms: dict[str, int] = profile["weighted_terms"]
        source_hints: set[str] = profile["source_hints"]
        raw_terms: set[str] = profile["raw_terms"]
        title_hints: set[str] = profile["title_hints"]
        scope = scope or {}

        candidate_boosts: dict[int, int] = {}
        with _connect(self.path) as connection:
            normalized_terms = [
                value
                for value in normalize_search_text(question).split()
                if len(value) >= 2
            ]
            if normalized_terms:
                expression = " OR ".join(
                    f'"{value.replace(chr(34), chr(34) * 2)}"'
                    for value in normalized_terms[:40]
                )
                try:
                    fts_rows = connection.execute(
                        """
                        SELECT CAST(rule_id AS INTEGER) AS rule_id,
                               bm25(rules_fts_v2, 0.0, 8.0, 1.0, 3.0, 4.0) AS rank
                        FROM rules_fts_v2
                        WHERE rules_fts_v2 MATCH ?
                        ORDER BY rank
                        LIMIT 500
                        """,
                        (expression,),
                    ).fetchall()
                    for index, item in enumerate(fts_rows):
                        candidate_boosts[int(item["rule_id"])] = max(
                            5, 45 - index // 12
                        )
                except sqlite3.OperationalError:
                    candidate_boosts = {}

            if source_hints:
                placeholders = ",".join("?" for _ in source_hints)
                source_rows = connection.execute(
                    f"""
                    SELECT id FROM rule_versions
                    WHERE source_key IN ({placeholders})
                    """,
                    tuple(sorted(source_hints)),
                ).fetchall()
                for item in source_rows:
                    candidate_boosts.setdefault(int(item["id"]), 12)

            clauses: list[str] = []
            params: list[Any] = []
            if as_of_date:
                clauses.extend(
                    (
                        "rv.status IN ('current', 'superseded', 'withdrawn', 'pending')",
                        "COALESCE(rv.valid_from, rv.effective_at, rv.published_at, "
                        "substr(rv.created_at, 1, 10)) <= ?",
                        "(rv.valid_to IS NULL OR rv.valid_to > ?)",
                    )
                )
                params.extend((as_of_date, as_of_date))
            else:
                clauses.append("rv.status='current'")
            if candidate_boosts:
                placeholders = ",".join("?" for _ in candidate_boosts)
                clauses.append(f"rv.id IN ({placeholders})")
                params.extend(candidate_boosts)

            scope_columns = ", ".join(
                f"sc.{field} AS scope_{field}" for field in SCOPE_FIELDS
            )
            sql = f"""
                SELECT rv.*,
                       sc.platform AS scope_platform,
                       {scope_columns},
                       sc.account_scoped AS scope_account_scoped,
                       sc.qualifiers_json AS scope_qualifiers_json
                FROM rule_versions AS rv
                LEFT JOIN applicability_scopes AS sc ON sc.id=rv.scope_id
                WHERE {" AND ".join(clauses)}
                ORDER BY rv.verified_at DESC, rv.id DESC
            """
            if not candidate_boosts:
                sql += " LIMIT 5000"
            rows = connection.execute(sql, params).fetchall()

        noise_titles = {"table of contents", "contents", "sections", "содержание", "目录"}
        scored: list[tuple[int, sqlite3.Row, list[str]]] = []
        for row in rows:
            if scope and not _row_matches_scope(row, scope):
                continue
            title = str(row["title"]).lower().strip()
            if title in noise_titles:
                continue
            source_key = str(row["source_key"])
            topic = str(row["topic"]).lower()
            content = str(row["content"]).lower()
            reasons: list[str] = []
            score = candidate_boosts.get(int(row["id"]), 0)
            if score:
                reasons.append(f"bm25:{score}")

            source_hit = source_key in source_hints
            if source_hit:
                score += 60
                reasons.append(f"source:{source_key}")
            matched_title_hints = [hint for hint in title_hints if hint in title]
            title_key = title.rstrip(" ?!:")
            if matched_title_hints:
                score += sum(80 if hint in {title, title_key} else 30 for hint in matched_title_hints)
                reasons.extend(f"title:{hint}" for hint in sorted(matched_title_hints))

            matched_terms: list[str] = []
            for token, weight in weighted_terms.items():
                title_count = title.count(token)
                topic_count = topic.count(token)
                content_count = min(content.count(token), 8)
                if title_count or topic_count or content_count:
                    matched_terms.append(token)
                    score += weight * (
                        title_count * 10 + topic_count * 7 + content_count
                    )
            if matched_terms:
                reasons.append("terms:" + ",".join(sorted(matched_terms)[:8]))

            matched_raw_count = sum(token in raw_terms for token in matched_terms)
            if profile["matched_concepts"] and source_hints and not (
                source_hit or matched_title_hints or matched_raw_count >= 2
            ):
                continue
            if not (source_hit or matched_title_hints or matched_terms):
                continue
            if score < 6:
                continue
            scored.append((score, row, reasons))

        scored.sort(
            key=lambda item: (item[0], item[1]["verified_at"], item[1]["id"]),
            reverse=True,
        )
        results: list[dict[str, Any]] = []
        for score, row, reasons in scored[:limit]:
            public = self._public_rule(row, score)
            public["match_reasons"] = reasons
            public["as_of_date"] = as_of_date
            public["evidence"] = self.evidence_for_rule(int(row["id"]))
            results.append(public)
        return results

    def history(self, rule_key: str) -> list[dict[str, Any]]:
        with _connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM rule_versions
                WHERE rule_key=?
                ORDER BY version DESC, id DESC
                """,
                (rule_key,),
            ).fetchall()
        results = [self._public_rule(row) for row in rows]
        for result in results:
            result["evidence"] = self.evidence_for_rule(int(result["id"]))
        return results

    def changes(self, since_days: int = 1) -> list[dict[str, Any]]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(0, since_days))
        ).replace(microsecond=0).isoformat()
        with _connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM rule_versions
                WHERE created_at >= ?
                ORDER BY created_at DESC, id DESC
                """,
                (cutoff,),
            ).fetchall()
        return [self._public_rule(row) for row in rows]

    def review_required(self) -> list[dict[str, Any]]:
        with _connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM rule_versions
                WHERE status='review_required'
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        return [self._public_rule(row) for row in rows]

    def evidence_for_rule(self, rule_version_id: int) -> list[dict[str, Any]]:
        with _connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT el.snapshot_id, el.section_id, el.source_url,
                       el.fragment, el.content_hash, el.linked_at,
                       sn.snapshot_path, sn.fetched_at, sn.raw_content_hash,
                       es.parser_version
                FROM evidence_links AS el
                JOIN snapshots AS sn ON sn.id=el.snapshot_id
                LEFT JOIN extracted_sections AS es ON es.id=el.section_id
                WHERE el.rule_version_id=?
                ORDER BY el.id DESC
                LIMIT 5
                """,
                (rule_version_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_review_decision(
        self,
        rule_version_id: int,
        decision: str,
        reason: str,
        reviewer: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject", "withdraw", "keep_current"}:
            raise ValueError("未知复核决定")
        if not reason.strip() or not reviewer.strip():
            raise ValueError("复核原因和复核人不得为空")
        with _connect(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM rule_versions WHERE id=?",
                (rule_version_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"规则版本不存在: {rule_version_id}")
            decided_at = utc_now()
            connection.execute(
                """
                INSERT INTO review_decisions(
                    rule_version_id, decision, reason, reviewer, notes, decided_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_version_id,
                    decision,
                    reason.strip(),
                    reviewer.strip(),
                    notes,
                    decided_at,
                ),
            )
            if decision == "approve":
                current = connection.execute(
                    """
                    SELECT id FROM rule_versions
                    WHERE rule_key=? AND scope_id=? AND status='current' AND id<>?
                    ORDER BY version DESC, id DESC LIMIT 1
                    """,
                    (row["rule_key"], row["scope_id"], rule_version_id),
                ).fetchone()
                if current:
                    boundary = (
                        row["valid_from"]
                        or row["effective_at"]
                        or row["published_at"]
                        or decided_at[:10]
                    )
                    connection.execute(
                        """
                        UPDATE rule_versions
                        SET status='superseded', valid_to=?
                        WHERE id=?
                        """,
                        (boundary, current["id"]),
                    )
                    connection.execute(
                        """
                        UPDATE effective_intervals
                        SET valid_to=?, retired_at=?
                        WHERE rule_version_id=?
                        """,
                        (boundary, decided_at, current["id"]),
                    )
                    self._remove_fts(connection, int(current["id"]))
                connection.execute(
                    """
                    UPDATE rule_versions
                    SET status='current', review_reason=NULL, supersedes_id=?
                    WHERE id=?
                    """,
                    (int(current["id"]) if current else None, rule_version_id),
                )
                self._add_fts(
                    connection,
                    rule_version_id,
                    str(row["title"]),
                    str(row["content"]),
                    str(row["topic"]),
                )
            else:
                connection.execute(
                    """
                    UPDATE rule_versions
                    SET status='withdrawn', review_reason=?
                    WHERE id=?
                    """,
                    (f"review_{decision}", rule_version_id),
                )
                self._remove_fts(connection, rule_version_id)
            revision = self._bump_revision(connection)
        return {
            "rule_version_id": rule_version_id,
            "decision": decision,
            "reviewer": reviewer.strip(),
            "decided_at": decided_at,
            "database_revision": revision,
        }

    def status(self) -> dict[str, Any]:
        with _connect(self.path) as connection:
            sources = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT source_key, url, topic, risk, last_verified_at,
                           last_error, etag, last_modified, last_http_status,
                           last_fetch_run_id
                    FROM sources ORDER BY source_key
                    """
                ).fetchall()
            ]
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM rule_versions GROUP BY status
                    """
                ).fetchall()
            }
            last_sync = connection.execute(
                """
                SELECT id, started_at, finished_at, mode, ok, result_json,
                       schema_version, source_keys_json, database_revision
                FROM sync_runs ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            metadata = {
                str(row["key"]): str(row["value"])
                for row in connection.execute(
                    """
                    SELECT key, value FROM metadata
                    WHERE key IN (
                        'schema_version', 'database_revision', 'fts5', 'fts5_v2'
                    )
                    """
                ).fetchall()
            }
            table_counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "snapshots",
                    "extracted_sections",
                    "effective_intervals",
                    "evidence_links",
                    "fetch_runs",
                    "review_decisions",
                )
            }
        return {
            "database": str(self.path),
            "schema_version": int(metadata.get("schema_version", "1")),
            "database_revision": int(metadata.get("database_revision", "0")),
            "search": {
                "fts5": metadata.get("fts5") == "true",
                "fts5_v2": metadata.get("fts5_v2") == "true",
            },
            "table_counts": table_counts,
            "sources": sources,
            "rule_counts": counts,
            "last_sync": dict(last_sync) if last_sync else None,
        }

    @staticmethod
    def _public_rule(row: sqlite3.Row, score: int | None = None) -> dict[str, Any]:
        available = set(row.keys())
        result = {
            key: row[key]
            for key in (
                "id",
                "rule_key",
                "source_key",
                "version",
                "status",
                "title",
                "content",
                "market",
                "seller_type",
                "fulfillment",
                "topic",
                "risk",
                "source_type",
                "source_url",
                "published_at",
                "effective_at",
                "verified_at",
                "created_at",
                "account_scoped",
                "review_reason",
            )
        }
        for key in (
            "scope_id",
            "section_id",
            "valid_from",
            "valid_to",
            "observed_at",
        ):
            if key in available:
                result[key] = row[key]
        applicability: dict[str, Any] = {}
        for field in SCOPE_FIELDS:
            alias = f"scope_{field}"
            if alias in available:
                applicability[field] = row[alias]
            elif field in available and field in {
                "market",
                "seller_type",
                "fulfillment",
            }:
                applicability[field] = row[field]
        if applicability:
            result["applicability"] = applicability
        if score is not None:
            result["match_score"] = score
        return result









