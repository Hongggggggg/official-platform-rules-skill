from __future__ import annotations

from core.adapter import ConfiguredAdapter
from core.fetch import FetchError, fetch_rendered_url
from core.html_extract import extract_document
from core.models import FetchResult, SourceDefinition
from core.sources import validate_official_url


class OzonAdapter(ConfiguredAdapter):
    platform = "ozon"

    def fetch(
        self,
        source: SourceDefinition,
        timeout: int = 30,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        try:
            result = super().fetch(
                source,
                timeout=timeout,
                etag=etag,
                last_modified=last_modified,
            )
        except FetchError:
            result = None
        if result is not None and result.status == 304:
            return result
        if result is not None:
            try:
                extract_document(result.body, result.content_type)
                return result
            except ValueError as exc:
                if "正文不足" not in str(exc):
                    raise
        render_url = result.url if result is not None else source.url
        # The browser fallback uses a reduced Chromium sandbox because of a
        # Windows GPU-cache failure. Re-validate immediately before launch so
        # only the configured first-party allowlist can reach that process.
        render_url = validate_official_url(
            render_url, self.config["official_domains"]
        )
        rendered = fetch_rendered_url(render_url, timeout=timeout)
        validate_official_url(rendered.url, self.config["official_domains"])
        extract_document(rendered.body, rendered.content_type)
        return rendered

