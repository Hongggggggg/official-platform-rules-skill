from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_json, load_platform_config
from .db import RuleDatabase
from .html_extract import extract_document
from .locking import platform_sync_lock
from .models import SourceDefinition, utc_now
from .schema_v2 import SCHEMA_VERSION
from .sources import content_hash, ensure_inside, safe_filename


class PlatformError(ValueError):
    pass


class RuleService:
    def __init__(self, root: Path, platform: str) -> None:
        self.root = root.resolve()
        registry = load_json(self.root / "config" / "platforms.json")
        entry = registry.get("platforms", {}).get(platform)
        if not entry or not entry.get("enabled"):
            raise PlatformError(f"平台未启用或仅预留: {platform}")
        self.platform = platform
        self.entry = entry
        self.config = load_platform_config(self.root, entry["config"])
        if self.config["data_namespace"] != entry["data_namespace"]:
            raise PlatformError("平台注册表与配置的数据命名空间不一致")
        module_name, class_name = entry["adapter"].split(":", 1)
        adapter_class = getattr(importlib.import_module(module_name), class_name)
        self.adapter = adapter_class(self.config)
        self.data_root = ensure_inside(
            self.root, self.root / "data" / entry["data_namespace"]
        )
        self.db = RuleDatabase(
            self.data_root / "rules.sqlite3",
            platform=self.platform,
        )
        self._initialized = False
        self._initialization_result: dict[str, Any] | None = None

    def initialize(self) -> dict[str, Any]:
        if self._initialized and self._initialization_result is not None:
            return self._initialization_result
        migration = self.db.initialize()
        sources = self.adapter.sources()
        for source in sources:
            self.db.upsert_source(source)
        activation = self.db.activate_due_pending()
        result = {
            "platform": self.platform,
            "database": str(self.db.path),
            "sources_registered": len(sources),
            "pending_activation": activation,
            "migration": migration,
        }
        self._initialized = True
        self._initialization_result = result
        return result

    def _snapshot_path(self, source: SourceDefinition, fetched_at: str, body: bytes) -> Path:
        date_part = fetched_at[:10]
        digest = content_hash(body)[:16]
        filename = f"{safe_filename(source.source_key)}-{digest}.html"
        return ensure_inside(
            self.data_root,
            self.data_root / "snapshots" / date_part / filename,
        )

    def sync(
        self,
        source_keys: set[str] | None = None,
        timeout: int = 30,
        mode: str = "incremental",
    ) -> dict[str, Any]:
        with platform_sync_lock(self.data_root):
            return self._sync_locked(source_keys, timeout, mode)

    def _sync_locked(
        self,
        source_keys: set[str] | None,
        timeout: int,
        mode: str,
    ) -> dict[str, Any]:
        init_result = self.initialize()
        available = {source.source_key: source for source in self.adapter.sources()}
        unknown = sorted((source_keys or set()) - available.keys())
        if unknown:
            raise PlatformError(f"未知来源: {unknown}")
        selected = [
            source for key, source in available.items()
            if source_keys is None or key in source_keys
        ]
        run_id = self.db.start_sync(
            mode,
            (source.source_key for source in selected),
        )
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for source in selected:
            fetch_run_id = self.db.start_fetch(source.source_key, run_id)
            try:
                cache = (
                    {"etag": None, "last_modified": None}
                    if mode == "full"
                    else self.db.source_cache_headers(source.source_key)
                )
                fetched = self.adapter.fetch(
                    source,
                    timeout=timeout,
                    etag=cache["etag"],
                    last_modified=cache["last_modified"],
                )
                if fetched.status == 304:
                    result = self.db.record_not_modified(
                        source.source_key,
                        fetched.fetched_at,
                        fetch_run_id,
                        etag=fetched.etag,
                        last_modified=fetched.last_modified,
                    )
                    self.db.finish_fetch(
                        fetch_run_id,
                        outcome="not_modified",
                        http_status=304,
                        final_url=fetched.url,
                        etag=fetched.etag,
                        last_modified=fetched.last_modified,
                    )
                    results.append(result)
                    continue
                document = extract_document(fetched.body, fetched.content_type)
                snapshot = self._snapshot_path(source, fetched.fetched_at, fetched.body)
                if self.db.latest_snapshot_hash(source.source_key) != content_hash(document.text):
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    if not snapshot.exists():
                        snapshot.write_bytes(fetched.body)
                relative = snapshot.relative_to(self.root).as_posix()
                result = self.db.ingest(
                    source,
                    document,
                    relative,
                    fetched.status,
                    fetched.fetched_at,
                    fetch_run_id=fetch_run_id,
                    raw_body=fetched.body,
                )
                self.db.finish_fetch(
                    fetch_run_id,
                    outcome=(
                        "changed"
                        if result["status"] in {"new", "changed"}
                        else "unchanged"
                    ),
                    http_status=fetched.status,
                    final_url=fetched.url,
                    etag=fetched.etag,
                    last_modified=fetched.last_modified,
                    page_hash=str(result["content_hash"]),
                    snapshot_id=int(result["snapshot_id"]),
                )
                results.append(result)
            except Exception as exc:  # each platform/source must fail independently
                message = f"{type(exc).__name__}: {exc}"
                self.db.record_source_error(source.source_key, message)
                self.db.finish_fetch(
                    fetch_run_id,
                    outcome="error",
                    error=message,
                )
                errors.append({"source_key": source.source_key, "error": message})
        activation = self.db.activate_due_pending()
        result = {
            "platform": self.platform,
            "schema_version": SCHEMA_VERSION,
            "sync_run_id": run_id,
            "mode": mode,
            "selected": len(selected),
            "results": results,
            "errors": errors,
            "ok": not errors,
            "pending_activation": activation,
            "init": init_result,
        }
        result["database_revision"] = self.db.database_revision()
        self.db.finish_sync(run_id, not errors, result)
        return result

    @staticmethod
    def _age_hours(value: str) -> float:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 3600)

    def _platform_conflict(self, question: str) -> str | None:
        lowered = question.lower()
        aliases = {
            "tiktok": ("tiktok", "tik tok"),
            "ozon": ("ozon",),
            "amazon": ("amazon", "亚马逊"),
            "shopee": ("shopee", "虾皮"),
            "shein": ("shein",),
        }
        selected_present = any(
            alias in lowered for alias in aliases.get(self.platform, ())
        )
        if selected_present:
            return None
        for platform, values in aliases.items():
            if platform == self.platform:
                continue
            if any(alias in lowered for alias in values):
                return platform
        return None

    def import_official_file(
        self,
        source_key: str,
        file_path: Path,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        with platform_sync_lock(self.data_root):
            return self._import_official_file_locked(
                source_key,
                file_path,
                content_type,
            )

    def _import_official_file_locked(
        self,
        source_key: str,
        file_path: Path,
        content_type: str | None,
    ) -> dict[str, Any]:
        self.initialize()
        source_map = {source.source_key: source for source in self.adapter.sources()}
        if source_key not in source_map:
            raise PlatformError(f"未知来源: {source_key}")
        source_path = ensure_inside(self.root, file_path.resolve())
        if not source_path.is_file():
            raise PlatformError(f"导入文件不存在: {source_path}")
        body = source_path.read_bytes()
        if len(body) > 8 * 1024 * 1024:
            raise PlatformError("官方导出文件超过 8 MiB 安全限制")
        if content_type is None:
            content_type = "text/plain" if source_path.suffix.lower() == ".txt" else "text/html"
        document = extract_document(body, content_type)
        fetched_at = utc_now()
        source = source_map[source_key]
        fetch_run_id = self.db.start_fetch(source_key, None)
        snapshot = self._snapshot_path(source, fetched_at, body)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not snapshot.exists():
            snapshot.write_bytes(body)
        try:
            result = self.db.ingest(
                source,
                document,
                snapshot.relative_to(self.root).as_posix(),
                200,
                fetched_at,
                fetch_run_id=fetch_run_id,
                raw_body=body,
            )
            self.db.finish_fetch(
                fetch_run_id,
                outcome=(
                    "changed"
                    if result["status"] in {"new", "changed"}
                    else "unchanged"
                ),
                http_status=200,
                final_url=source.url,
                page_hash=str(result["content_hash"]),
                snapshot_id=int(result["snapshot_id"]),
            )
        except Exception as exc:
            self.db.finish_fetch(
                fetch_run_id,
                outcome="error",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        return {
            "platform": self.platform,
            "imported_from": source_path.relative_to(self.root).as_posix(),
            "source_key": source_key,
            "database_revision": self.db.database_revision(),
            "result": result,
        }
    def query(
        self,
        question: str,
        limit: int = 8,
        scope: dict[str, str] | None = None,
        refresh_stale: bool = True,
        timeout: int = 30,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        conflicting_platform = self._platform_conflict(question)
        if conflicting_platform:
            return {
                "platform": self.platform,
                "question": question,
                "scope": scope or {},
                "as_of_date": as_of_date,
                "official_evidence_confirmed": False,
                "message": (
                    "官方资料暂未确认。问题指向 "
                    f"{conflicting_platform}，不能使用 {self.platform} 数据库作答。"
                ),
                "rules": [],
                "stale_source_keys": [],
                "refresh": None,
            }
        rules = self.db.search(
            question,
            limit=limit,
            scope=scope,
            as_of_date=as_of_date,
        )
        stale_keys: set[str] = set()
        for rule in rules:
            threshold = self.config["freshness"][
                "high_risk_hours" if rule["risk"] == "high" else "normal_hours"
            ]
            if self._age_hours(rule["verified_at"]) > threshold:
                stale_keys.add(rule["source_key"])
        refresh_result = None
        if refresh_stale and stale_keys:
            refresh_result = self.sync(stale_keys, timeout=timeout, mode="targeted")
            rules = self.db.search(
                question,
                limit=limit,
                scope=scope,
                as_of_date=as_of_date,
            )
        return {
            "platform": self.platform,
            "question": question,
            "scope": scope or {},
            "as_of_date": as_of_date,
            "official_evidence_confirmed": bool(rules),
            "message": None if rules else "官方资料暂未确认。当前官方来源库没有足够证据支持确定性结论。",
            "rules": rules,
            "stale_source_keys": sorted(stale_keys),
            "refresh": refresh_result,
        }

    def digest(self, since_days: int) -> dict[str, Any]:
        self.initialize()
        changes = self.db.changes(since_days)
        return {"platform": self.platform, "since_days": since_days, "changes": changes}

    def history(self, rule_key: str) -> dict[str, Any]:
        self.initialize()
        return {"platform": self.platform, "rule_key": rule_key, "versions": self.db.history(rule_key)}

    def status(self) -> dict[str, Any]:
        self.initialize()
        return {"platform": self.platform, **self.db.status()}

    def review(self) -> dict[str, Any]:
        self.initialize()
        return {"platform": self.platform, "items": self.db.review_required()}

    def decide_review(
        self,
        rule_version_id: int,
        decision: str,
        reason: str,
        reviewer: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        return {
            "platform": self.platform,
            **self.db.record_review_decision(
                rule_version_id,
                decision,
                reason,
                reviewer,
                notes,
            ),
        }

