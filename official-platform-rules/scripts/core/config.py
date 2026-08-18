from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


REQUIRED_CONFIG_FIELDS = {
    "schema_version",
    "platform",
    "display_name",
    "market",
    "seller_origin",
    "data_namespace",
    "official_domains",
    "freshness",
    "scope_defaults",
    "sources",
}
SOURCE_FIELDS = {
    "source_key",
    "canonical_rule_key",
    "url",
    "source_type",
    "topic",
    "risk",
}
SOURCE_TYPES = {"contract", "policy", "specific_rule", "guide", "news"}
SCOPE_FIELDS = {
    "market",
    "seller_origin",
    "actor_type",
    "seller_type",
    "shop_type",
    "fulfillment",
    "category",
    "program",
    "order_state",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取配置 {path}: {exc}") from exc


def validate_platform_config(payload: dict[str, Any]) -> None:
    missing = REQUIRED_CONFIG_FIELDS - payload.keys()
    if missing:
        raise ConfigError(f"平台配置缺少字段: {sorted(missing)}")
    if payload["schema_version"] != 2:
        raise ConfigError("仅支持 schema_version=2")
    if not payload["official_domains"]:
        raise ConfigError("official_domains 不得为空")
    if payload["freshness"].get("high_risk_hours", 0) < 1:
        raise ConfigError("high_risk_hours 必须大于 0")
    if payload["freshness"].get("normal_hours", 0) < 1:
        raise ConfigError("normal_hours 必须大于 0")
    scope_defaults = payload["scope_defaults"]
    required_scope = {
        "market",
        "seller_origin",
        "actor_type",
        "seller_type",
        "fulfillment",
    }
    missing_scope = required_scope - scope_defaults.keys()
    if missing_scope:
        raise ConfigError(f"默认适用范围缺少字段: {sorted(missing_scope)}")
    unknown_scope = scope_defaults.keys() - SCOPE_FIELDS
    if unknown_scope:
        raise ConfigError(f"默认适用范围包含未知字段: {sorted(unknown_scope)}")
    seen: set[str] = set()
    for source in payload["sources"]:
        source_missing = SOURCE_FIELDS - source.keys()
        if source_missing:
            raise ConfigError(
                f"来源缺少字段 {source.get('source_key', '<unknown>')}: "
                f"{sorted(source_missing)}"
            )
        if source["source_key"] in seen:
            raise ConfigError(f"重复 source_key: {source['source_key']}")
        seen.add(source["source_key"])
        if source["source_type"] not in SOURCE_TYPES:
            raise ConfigError(f"未知 source_type: {source['source_type']}")
        if source["risk"] not in {"high", "normal"}:
            raise ConfigError(f"未知 risk: {source['risk']}")
        unknown_source_scope = source.get("scope", {}).keys() - SCOPE_FIELDS
        if unknown_source_scope:
            raise ConfigError(
                f"来源 {source['source_key']} 包含未知适用范围字段: "
                f"{sorted(unknown_source_scope)}"
            )


def load_platform_config(root: Path, filename: str) -> dict[str, Any]:
    payload = load_json(root / "config" / filename)
    validate_platform_config(payload)
    return payload


