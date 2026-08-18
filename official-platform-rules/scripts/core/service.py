from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapter import GenericPlatformAdapter
from .config import validate_platform_config
from .db import RuleDatabase
from .discovery import REQUIRED_TOPICS, discover
from .html_extract import extract_document
from .locking import platform_sync_lock
from .models import SourceDefinition, utc_now
from .profiles import ProfileStore
from .schema_v3 import SCHEMA_VERSION
from .sources import content_hash, ensure_inside, safe_filename


class PlatformError(ValueError):
    pass


class RuleService:
    def __init__(self, root: Path, profile_id: str) -> None:
        self.root = root.resolve()
        self.profile_store = ProfileStore(self.root)
        self.profile = self.profile_store.load(profile_id)
        if self.profile.get("status") == "archived":
            raise PlatformError(f"平台档案已归档: {profile_id}")
        if not self.profile.get("verified_domains"):
            raise PlatformError(
                "档案尚无已核验官方域名；请先发现并确认至少一个官方 HTTPS 入口"
            )
        self.platform = profile_id
        self.profile_id = profile_id
        self.config = self.profile_store.load_sources(profile_id)
        validate_platform_config(self.config)
        self.adapter = GenericPlatformAdapter(self.config)
        self.data_root = self.profile_store.profile_dir(profile_id)
        self.db = RuleDatabase(
            self.data_root / "rules.sqlite3",
            platform=self.profile_id,
        )
        self._initialized = False
        self._initialization_result: dict[str, Any] | None = None

    def initialize(self) -> dict[str, Any]:
        if self._initialized and self._initialization_result is not None:
            return self._initialization_result
        migration = self.db.initialize()
        self.db.upsert_profile_metadata(self.profile)
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

    def _reload_sources(self) -> None:
        self.config = self.profile_store.load_sources(self.profile_id)
        validate_platform_config(self.config)
        self.adapter = GenericPlatformAdapter(self.config)
        self._initialized = False
        self._initialization_result = None

    def discover(self, timeout: int = 30, max_pages: int = 1000) -> dict[str, Any]:
        self.initialize()
        run_id = self.db.start_discovery("full")
        try:
            result = discover(
                list(self.profile["official_seed_urls"]),
                set(self.profile["verified_domains"]),
                timeout=timeout,
                max_pages=max(1, min(max_pages, 10000)),
            )
            for candidate in result["candidates"]:
                self.db.upsert_source_candidate(candidate)
            existing = {item["url"]: item for item in self.config["sources"]}
            for item in result["sources"]:
                existing[item["url"]] = item
            self.profile_store.save_sources(
                self.profile_id,
                sorted(existing.values(), key=lambda item: item["source_key"]),
            )
            self.profile_store.mark_run(self.profile_id, discovery=True)
            self.profile = self.profile_store.load(self.profile_id)
            self._reload_sources()
            payload = {
                "profile_id": self.profile_id,
                "discovery_run_id": run_id,
                **result,
                "source_count": len(existing),
                "ok": not result["errors"],
            }
            self.db.finish_discovery(
                run_id,
                payload["ok"],
                {
                    "profile_id": self.profile_id,
                    "visited": result["visited"],
                    "truncated": result["truncated"],
                    "source_count": len(existing),
                    "errors": result["errors"],
                    "warnings": result.get("warnings", []),
                    "ok": payload["ok"],
                },
            )
            return payload
        except Exception as exc:
            self.db.finish_discovery(
                run_id, False, {"error": f"{type(exc).__name__}: {exc}"}
            )
            raise

    def build(self, timeout: int = 30, max_pages: int = 1000) -> dict[str, Any]:
        self.profile["status"] = "building"
        self.profile_store.save(self.profile)
        discovery = self.discover(timeout=timeout, max_pages=max_pages)
        self._reload_sources()
        sources = self.adapter.sources()
        priority = {source.source_key for source in sources if source.risk == "high"}
        high_risk = self.sync(priority, timeout=timeout, mode="initial-high-risk") if priority else None
        remaining = {source.source_key for source in sources if source.source_key not in priority}
        normal = self.sync(remaining, timeout=timeout, mode="initial-complete") if remaining else None
        self.profile_store.mark_run(self.profile_id)
        coverage = self.coverage(record=True)
        self.profile = self.profile_store.load(self.profile_id)
        self.profile["status"] = coverage["status"]
        self.profile_store.save(self.profile)
        self.db.upsert_profile_metadata(self.profile)
        return {
            "profile_id": self.profile_id,
            "discovery": discovery,
            "high_risk_sync": high_risk,
            "remaining_sync": normal,
            "coverage": coverage,
            "ok": bool((not high_risk or high_risk["ok"]) and (not normal or normal["ok"])),
        }

    def update(self, timeout: int = 30, rediscover: bool | None = None) -> dict[str, Any]:
        if rediscover is None:
            rediscover = self.profile_store.is_discovery_due(self.profile_id)
        discovery_result = self.discover(timeout=timeout, max_pages=1000) if rediscover else None
        self._reload_sources()
        sync_result = self.sync(timeout=timeout, mode="daily-incremental")
        self.profile_store.mark_run(self.profile_id)
        self.profile = self.profile_store.load(self.profile_id)
        coverage = self.coverage(record=True)
        self.profile["status"] = coverage["status"]
        self.profile_store.save(self.profile)
        self.db.upsert_profile_metadata(self.profile)
        return {
            "profile_id": self.profile_id,
            "rediscovery": discovery_result,
            "sync": sync_result,
            "coverage": coverage,
            "ok": sync_result["ok"],
        }

    def coverage(self, record: bool = True) -> dict[str, Any]:
        self.initialize()
        status = self.db.status()
        candidates = self.db.source_candidates()
        latest_discovery = self.db.latest_discovery()
        discovery_result = (latest_discovery or {}).get("result", {})
        sources = status["sources"]
        topics = sorted({str(item["topic"]) for item in sources if item.get("last_verified_at")})
        missing = [topic for topic in REQUIRED_TOPICS if topic not in topics]
        failed = [item for item in sources if item.get("last_error")]
        pending = [item for item in candidates if item["status"] == "pending"]
        dynamic_shell = [item for item in candidates if item["status"] == "dynamic_shell"]
        login_required = [item for item in candidates if item["status"] == "login_required"]
        rejected = [item for item in candidates if item["status"] == "rejected"]
        candidate_errors = [item for item in candidates if item["status"] == "error"]
        fetched = [item for item in sources if item.get("last_verified_at")]
        now = datetime.now(timezone.utc)
        stale: list[str] = []
        for item in fetched:
            threshold = self.config["freshness"][
                "high_risk_hours" if item["risk"] == "high" else "normal_hours"
            ]
            parsed = datetime.fromisoformat(item["last_verified_at"].replace("Z", "+00:00"))
            if (now - parsed).total_seconds() / 3600 > threshold:
                stale.append(item["source_key"])
        discovery_truncated = bool(discovery_result.get("truncated", False))
        discovery_errors = list(discovery_result.get("errors", []))
        discovery_ok = bool(latest_discovery and latest_discovery.get("ok"))
        complete = (
            bool(sources) and discovery_ok and not discovery_truncated
            and not discovery_errors and not missing and not failed
            and not pending and not stale
        )
        report = {
            "profile_id": self.profile_id,
            "status": "complete" if complete else "partial",
            "discovered": len(candidates),
            "accepted": sum(item["status"] not in {"pending", "rejected"} for item in candidates),
            "fetched": len(fetched),
            "pending_review": len(pending),
            "failed": len(failed),
            "dynamic_shell": len(dynamic_shell),
            "login_required": len(login_required),
            "rejected": len(rejected),
            "candidate_errors": len(candidate_errors),
            "discovery_truncated": discovery_truncated,
            "discovery_errors": discovery_errors,
            "stale": stale,
            "covered_topics": topics,
            "missing_topics": missing,
            "truly_latest": complete,
            "limitations": [] if complete else ["未达到全面性或新鲜度门槛，不得声称全面或最新"],
        }
        if record:
            report["coverage_audit_id"] = self.db.record_coverage(report)
        return report

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
                    self.db.mark_candidate_status(source.url, "fetched")
                    continue
                try:
                    document = extract_document(fetched.body, fetched.content_type)
                except ValueError as extraction_error:
                    message = str(extraction_error)
                    if not any(
                        marker in message
                        for marker in ("动态空壳", "正文不足", "没有可用")
                    ):
                        raise
                    fetched = self.adapter.fetch_rendered(source, timeout=timeout)
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
                self.db.mark_candidate_status(source.url, "fetched")
            except Exception as exc:  # each platform/source must fail independently
                message = f"{type(exc).__name__}: {exc}"
                self.db.record_source_error(source.source_key, message)
                lowered = message.lower()
                if any(
                    marker in lowered
                    for marker in ("登录", "login", "captcha", "验证码")
                ):
                    candidate_status = "login_required"
                elif any(
                    marker in lowered
                    for marker in ("动态", "正文不足", "empty", "shell")
                ):
                    candidate_status = "dynamic_shell"
                else:
                    candidate_status = "error"
                self.db.mark_candidate_status(
                    source.url, candidate_status, message
                )
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
        selected = str(self.profile["platform_name"]).lower()
        if selected in lowered:
            return None
        for item in self.profile_store.list():
            if item["profile_id"] == self.profile_id:
                continue
            name = str(item["platform_name"]).lower()
            if name and name in lowered:
                return str(item["profile_id"])
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
        overdue_update = None
        if refresh_stale and self.profile_store.is_update_due(self.profile_id):
            overdue_update = self.update(timeout=timeout)
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
            "overdue_update": overdue_update,
            "knowledge_base": self.coverage(record=False),
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
        return {
            "profile_id": self.profile_id,
            "platform_name": self.profile["platform_name"],
            "profile_status": self.profile.get("status"),
            "update_due": self.profile_store.is_update_due(self.profile_id),
            "discovery_due": self.profile_store.is_discovery_due(self.profile_id),
            "knowledge_base": self.coverage(record=False),
            **self.db.status(),
        }

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

