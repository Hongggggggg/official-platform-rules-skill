from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_platform_config
from .profiles import ProfileStore
from .sources import SourceRejected, validate_official_url


def audit_skill(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    namespaces: dict[str, str] = {}
    enabled: list[str] = []
    store = ProfileStore(root)
    profiles = store.list()
    if not profiles:
        warnings.append("尚无运行时平台档案；首次使用必须先 onboard")
    for profile in profiles:
        profile_id = str(profile.get("profile_id", ""))
        if not profile_id:
            errors.append("发现缺少 profile_id 的运行时档案")
            continue
        if profile.get("status") == "archived":
            continue
        enabled.append(profile_id)
        namespace = profile_id
        if namespace in namespaces:
            errors.append(f"数据命名空间重复: {namespace}")
        namespaces[namespace] = str(profile.get("platform_name", profile_id))
        if not profile.get("verified_domains"):
            warnings.append(f"{profile_id}: 尚无已核验官方域名")
            continue
        try:
            config_path = store.profile_dir(profile_id) / "sources.json"
            config = load_platform_config(config_path.parent, config_path.name)
        except Exception as exc:
            errors.append(f"{profile_id} 配置无效: {exc}")
            continue
        if config["platform"] != profile_id:
            errors.append(f"{profile_id} 配置中的 platform 不一致")
        if config["data_namespace"] != namespace:
            errors.append(f"{profile_id} 数据命名空间不一致")
        for source in config["sources"]:
            try:
                validate_official_url(source["url"], config["official_domains"])
            except SourceRejected as exc:
                errors.append(f"{profile_id}/{source['source_key']}: {exc}")
    required = (root / "SKILL.md", root / "agents" / "openai.yaml", root / "scripts" / "cli.py")
    for path in required:
        if not path.is_file():
            errors.append(f"Skill 必需文件不存在: {path}")
    return {
        "ok": not errors,
        "dynamic_profiles": enabled,
        "active_profile": store.active(),
        "isolated_namespaces": namespaces,
        "errors": errors,
        "warnings": warnings,
    }
