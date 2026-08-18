from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class SourceDefinition:
    source_key: str
    canonical_rule_key: str
    url: str
    source_type: str
    topic: str
    risk: str
    scope: dict[str, str] = field(default_factory=dict)
    account_scoped: bool = False

    @classmethod
    def from_dict(
        cls, payload: dict[str, Any], defaults: dict[str, str]
    ) -> "SourceDefinition":
        return cls(
            source_key=payload["source_key"],
            canonical_rule_key=payload["canonical_rule_key"],
            url=payload["url"],
            source_type=payload["source_type"],
            topic=payload["topic"],
            risk=payload["risk"],
            scope={**defaults, **payload.get("scope", {})},
            account_scoped=bool(payload.get("account_scoped", False)),
        )


@dataclass(frozen=True)
class ExtractedDocument:
    title: str
    language: str
    text: str
    sections: tuple[tuple[str, str], ...]
    published_at: str | None = None
    effective_at: str | None = None


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    content_type: str
    body: bytes
    fetched_at: str
    etag: str | None = None
    last_modified: str | None = None

