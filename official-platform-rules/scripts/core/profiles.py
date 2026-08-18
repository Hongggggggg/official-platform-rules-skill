from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .models import utc_now
from .sources import ensure_inside


PROFILE_SCHEMA_VERSION = 1


class ProfileError(ValueError):
    pass


def _slug(value: str, fallback: str = "platform") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (normalized or fallback)[:36]


def normalize_https_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ProfileError(f"官方入口必须是完整 HTTPS URL: {value}")
    if parsed.username or parsed.password:
        raise ProfileError("官方入口不得包含用户名或密码")
    return parsed._replace(fragment="").geturl()


def profile_identity(
    platform_name: str,
    market: str,
    seller_origin: str,
    actor_type: str,
    seller_type: str,
    fulfillment: str,
) -> str:
    values = (
        platform_name.strip().casefold(),
        market.strip().casefold(),
        seller_origin.strip().casefold(),
        actor_type.strip().casefold(),
        seller_type.strip().casefold(),
        fulfillment.strip().casefold(),
    )
    digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:10]
    return f"{_slug(platform_name)}-{_slug(market, 'global')}-{digest}"


class ProfileStore:
    def __init__(self, skill_root: Path) -> None:
        self.skill_root = skill_root.resolve()
        self.root = ensure_inside(
            self.skill_root, self.skill_root / "data" / "profiles"
        )

    def profile_dir(self, profile_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", profile_id):
            raise ProfileError(f"无效 profile_id: {profile_id}")
        return ensure_inside(self.root, self.root / profile_id)

    def _profile_path(self, profile_id: str) -> Path:
        return self.profile_dir(profile_id) / "profile.json"

    def _sources_path(self, profile_id: str) -> Path:
        return self.profile_dir(profile_id) / "sources.json"

    @property
    def active_path(self) -> Path:
        return self.root / "active_profile.txt"

    def create(
        self,
        *,
        platform_name: str,
        market: str,
        seller_origin: str = "unspecified",
        actor_type: str = "seller",
        seller_type: str = "unspecified",
        fulfillment: str = "unspecified",
        official_urls: Iterable[str] = (),
        timezone_name: str = "Asia/Shanghai",
        daily_update_time: str = "03:00",
    ) -> dict[str, Any]:
        if not platform_name.strip() or not market.strip():
            raise ProfileError("平台名称和国家/站点不得为空")
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise ProfileError(f"无效时区: {timezone_name}") from exc
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_update_time):
            raise ProfileError("每日更新时间必须为 HH:MM")
        urls = list(dict.fromkeys(normalize_https_url(url) for url in official_urls))
        domains = sorted({urlparse(url).hostname.lower() for url in urls})
        profile_id = profile_identity(
            platform_name,
            market,
            seller_origin,
            actor_type,
            seller_type,
            fulfillment,
        )
        path = self._profile_path(profile_id)
        if path.exists():
            return self.load(profile_id)
        now = utc_now()
        payload = {
            "profile_schema_version": PROFILE_SCHEMA_VERSION,
            "profile_id": profile_id,
            "platform_name": platform_name.strip(),
            "display_name": f"{platform_name.strip()} {market.strip()}",
            "market": market.strip(),
            "seller_origin": seller_origin.strip() or "unspecified",
            "actor_type": actor_type.strip() or "seller",
            "seller_type": seller_type.strip() or "unspecified",
            "fulfillment": fulfillment.strip() or "unspecified",
            "official_seed_urls": urls,
            "verified_domains": domains,
            "status": "ready_for_discovery" if urls else "needs_official_sources",
            "schedule": {
                "timezone": timezone_name,
                "daily_update_time": daily_update_time,
                "daily_enabled": True,
                "weekly_rediscovery_day": 6,
                "last_update_at": None,
                "last_discovery_at": None,
            },
            "created_at": now,
            "updated_at": now,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        self.save(payload)
        self.save_sources(profile_id, [])
        self.activate(profile_id)
        return payload

    def load(self, profile_id: str) -> dict[str, Any]:
        path = self._profile_path(profile_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"无法读取平台档案 {profile_id}: {exc}") from exc
        if payload.get("profile_id") != profile_id:
            raise ProfileError("档案内部 profile_id 与目录不一致")
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        profile_id = str(payload["profile_id"])
        payload["updated_at"] = utc_now()
        path = self._profile_path(profile_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def list(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        active = self.active()
        result: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*/profile.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            item["active"] = item.get("profile_id") == active
            result.append(item)
        return result

    def active(self) -> str | None:
        if not self.active_path.exists():
            return None
        value = self.active_path.read_text(encoding="utf-8").strip()
        return value or None

    def activate(self, profile_id: str) -> None:
        self.load(profile_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.active_path.write_text(profile_id + "\n", encoding="utf-8")

    def add_official_urls(
        self, profile_id: str, official_urls: Iterable[str]
    ) -> dict[str, Any]:
        profile = self.load(profile_id)
        urls = list(
            dict.fromkeys(
                [
                    *profile.get("official_seed_urls", []),
                    *(normalize_https_url(url) for url in official_urls),
                ]
            )
        )
        domains = sorted(
            {
                *profile.get("verified_domains", []),
                *(urlparse(url).hostname.lower() for url in urls),
            }
        )
        existing_sources = self.load_sources(profile_id).get("sources", [])
        profile["official_seed_urls"] = urls
        profile["verified_domains"] = domains
        profile["status"] = "ready_for_discovery"
        self.save(profile)
        self.save_sources(profile_id, existing_sources)
        return self.load(profile_id)

    def delete(self, profile_id: str) -> dict[str, Any]:
        directory = self.profile_dir(profile_id)
        if not directory.exists():
            raise ProfileError(f"档案不存在: {profile_id}")
        # A profile contains material evidence. Do not recursively remove it here;
        # mark it archived so deletion remains recoverable and auditable.
        profile = self.load(profile_id)
        profile["status"] = "archived"
        profile["archived_at"] = utc_now()
        self.save(profile)
        if self.active() == profile_id:
            self.active_path.unlink(missing_ok=True)
        return {"profile_id": profile_id, "archived": True, "recoverable": True}

    def load_sources(self, profile_id: str) -> dict[str, Any]:
        profile = self.load(profile_id)
        path = self._sources_path(profile_id)
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"无法读取来源清单 {profile_id}: {exc}") from exc

    def save_sources(self, profile_id: str, sources: list[dict[str, Any]]) -> None:
        profile = self.load(profile_id)
        payload = {
            "schema_version": 2,
            "platform": profile_id,
            "display_name": profile["display_name"],
            "market": profile["market"],
            "seller_origin": profile["seller_origin"],
            "data_namespace": profile_id,
            "official_domains": profile["verified_domains"],
            "freshness": {"high_risk_hours": 24, "normal_hours": 168},
            "scope_defaults": {
                "market": profile["market"],
                "seller_origin": profile["seller_origin"],
                "actor_type": profile["actor_type"],
                "seller_type": profile["seller_type"],
                "fulfillment": profile["fulfillment"],
            },
            "sources": sources,
        }
        path = self._sources_path(profile_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def mark_run(self, profile_id: str, *, discovery: bool = False) -> None:
        profile = self.load(profile_id)
        key = "last_discovery_at" if discovery else "last_update_at"
        profile["schedule"][key] = utc_now()
        self.save(profile)

    def is_update_due(self, profile_id: str, now: datetime | None = None) -> bool:
        profile = self.load(profile_id)
        schedule = profile["schedule"]
        if not schedule.get("daily_enabled", True):
            return False
        zone = ZoneInfo(schedule.get("timezone", "Asia/Shanghai"))
        current = now.astimezone(zone) if now else datetime.now(zone)
        hour, minute = (int(value) for value in schedule["daily_update_time"].split(":"))
        due = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if current < due:
            due -= timedelta(days=1)
        last = schedule.get("last_update_at")
        if not last:
            return True
        return datetime.fromisoformat(last.replace("Z", "+00:00")).astimezone(zone) < due

    def is_discovery_due(self, profile_id: str, now: datetime | None = None) -> bool:
        profile = self.load(profile_id)
        last = profile["schedule"].get("last_discovery_at")
        if not last:
            return True
        parsed = datetime.fromisoformat(last.replace("Z", "+00:00"))
        current = now or datetime.now(parsed.tzinfo)
        return current - parsed >= timedelta(days=7)
