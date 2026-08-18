from __future__ import annotations

from typing import Any

from .fetch import FetchResult, fetch_url
from .models import SourceDefinition
from .sources import validate_official_url


class ConfiguredAdapter:
    """Platform-local configuration adapter; subclasses must live per platform."""

    platform = ""

    def __init__(self, config: dict[str, Any]) -> None:
        if config["platform"] != self.platform:
            raise ValueError(
                f"适配器 {self.platform} 不能加载 {config['platform']} 配置"
            )
        self.config = config

    def sources(self) -> tuple[SourceDefinition, ...]:
        defaults = self.config["scope_defaults"]
        allowed = self.config["official_domains"]
        result: list[SourceDefinition] = []
        for payload in self.config["sources"]:
            normalized = validate_official_url(payload["url"], allowed)
            result.append(
                SourceDefinition.from_dict({**payload, "url": normalized}, defaults)
            )
        return tuple(result)

    def fetch(
        self,
        source: SourceDefinition,
        timeout: int = 30,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        validate_official_url(source.url, self.config["official_domains"])
        result = fetch_url(
            source.url,
            timeout=timeout,
            etag=etag,
            last_modified=last_modified,
        )
        validate_official_url(result.url, self.config["official_domains"])
        return result

