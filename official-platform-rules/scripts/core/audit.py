from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_json, load_platform_config
from .sources import SourceRejected, validate_official_url


def audit_skill(root: Path) -> dict[str, Any]:
    registry = load_json(root / "config" / "platforms.json")
    errors: list[str] = []
    warnings: list[str] = []
    namespaces: dict[str, str] = {}
    enabled: list[str] = []
    for platform, entry in registry.get("platforms", {}).items():
        namespace = entry.get("data_namespace")
        if namespace in namespaces:
            errors.append(f"数据命名空间重复: {namespace} ({namespaces[namespace]}, {platform})")
        namespaces[namespace] = platform
        if not entry.get("enabled"):
            if entry.get("adapter") or entry.get("config"):
                warnings.append(f"预留平台 {platform} 已禁用但仍配置了实现")
            continue
        enabled.append(platform)
        try:
            config = load_platform_config(root, entry["config"])
        except Exception as exc:
            errors.append(f"{platform} 配置无效: {exc}")
            continue
        if config["platform"] != platform:
            errors.append(f"{platform} 配置中的 platform 不一致")
        if config["data_namespace"] != namespace:
            errors.append(f"{platform} 数据命名空间不一致")
        for source in config["sources"]:
            try:
                validate_official_url(source["url"], config["official_domains"])
            except SourceRejected as exc:
                errors.append(f"{platform}/{source['source_key']}: {exc}")
        adapter_module = entry["adapter"].split(":", 1)[0]
        adapter_path = root / "scripts" / Path(*adapter_module.split("."))
        adapter_file = adapter_path.with_suffix(".py")
        if not adapter_file.exists():
            errors.append(f"{platform} 适配器不存在: {adapter_file}")
            continue
        code = adapter_file.read_text(encoding="utf-8-sig").lower()
        for other in registry["platforms"]:
            if other != platform and other in enabled and (
                f"import {other}" in code or f"from {other}" in code
            ):
                errors.append(f"{platform} 适配器耦合到 {other}")
    expected_reserved = {"amazon", "shein"}
    missing_reserved = sorted(expected_reserved - registry.get("platforms", {}).keys())
    if missing_reserved:
        errors.append(f"缺少预留平台: {missing_reserved}")
    return {
        "ok": not errors,
        "enabled_platforms": enabled,
        "isolated_namespaces": namespaces,
        "errors": errors,
        "warnings": warnings,
    }
