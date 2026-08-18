from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class SourceRejected(ValueError):
    pass


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https":
        raise SourceRejected("只接受 HTTPS 官方来源")
    if not host or parts.username or parts.password:
        raise SourceRejected("URL 主机无效或包含凭证")
    port = f":{parts.port}" if parts.port and parts.port != 443 else ""
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(("https", host + port, path, query, ""))


def validate_official_url(url: str, allowed_domains: list[str]) -> str:
    normalized = normalize_url(url)
    host = (urlsplit(normalized).hostname or "").lower()
    allowed = any(
        host == domain.lower() or host.endswith("." + domain.lower())
        for domain in allowed_domains
    )
    if not allowed:
        raise SourceRejected(f"非官方白名单域名: {host}")
    return normalized


def content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def safe_filename(value: str, limit: int = 80) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return (value or "document")[:limit]


def ensure_inside(root: Path, target: Path) -> Path:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise ValueError(f"目标路径越界: {resolved_target}")
    return resolved_target

