from __future__ import annotations

import html
import re
from datetime import datetime
from html.parser import HTMLParser

from .models import ExtractedDocument


SKIP_TAGS = {"script", "style", "svg", "noscript", "template", "nav", "footer"}
BLOCK_TAGS = {
    "p",
    "li",
    "div",
    "section",
    "article",
    "tr",
    "br",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4"}


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.title_depth = 0
        self.heading_tag: str | None = None
        self.title_parts: list[str] = []
        self.tokens: list[tuple[str, str]] = []
        self.current: list[str] = []
        self.language = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = dict(attrs)
        if tag == "html":
            self.language = attrs_dict.get("lang") or ""
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.title_depth += 1
        if tag in HEADING_TAGS:
            self._flush("text")
            self.heading_tag = tag
        elif tag in BLOCK_TAGS:
            self._flush("text")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "title":
            self.title_depth = max(0, self.title_depth - 1)
        if tag in HEADING_TAGS:
            self._flush("heading")
            self.heading_tag = None
        elif tag in BLOCK_TAGS:
            self._flush("text")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        value = _clean(data)
        if not value:
            return
        if self.title_depth:
            self.title_parts.append(value)
        self.current.append(value)

    def close(self) -> None:
        super().close()
        self._flush("text")

    def _flush(self, kind: str) -> None:
        value = _clean(" ".join(self.current))
        self.current.clear()
        if value:
            self.tokens.append((kind, value))


def _clean(value: str) -> str:
    value = html.unescape(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _decode(body: bytes) -> str:
    head = body[:2048].decode("ascii", errors="ignore")
    match = re.search(r"charset=[\"']?([a-zA-Z0-9_-]+)", head, flags=re.I)
    encodings = [match.group(1)] if match else []
    encodings.extend(["utf-8", "windows-1251", "latin-1"])
    for encoding in encodings:
        try:
            return body.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def _iso_date(value: str) -> str | None:
    formats = [
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d.%m.%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


DATE_PATTERN = (
    r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|"
    r"[A-Z][a-z]+ \d{1,2}, \d{4}|\d{1,2}\.\d{1,2}\.\d{4})"
)


def _date_after(labels: tuple[str, ...], text: str) -> str | None:
    date_pattern = DATE_PATTERN
    for label in labels:
        match = re.search(
            re.escape(label) + r"[^0-9A-Z]{0,30}" + date_pattern,
            text,
            flags=re.I,
        )
        if match:
            return _iso_date(match.group(1))
    return None


def _leading_date(text: str) -> str | None:
    match = re.search(DATE_PATTERN, text[:500])
    return _iso_date(match.group(1)) if match else None

def _trim_ui_noise(content: str) -> str:
    lowered = content.lower()
    positions = [
        lowered.find(marker)
        for marker in ("\nhotkeys\ngeneral", "\nгорячие клавиши\nобщие")
        if lowered.find(marker) >= 0
    ]
    if positions:
        prefix = content[: min(positions)].strip()
        if len(prefix) >= 100:
            return prefix
    return content

def _is_noise_section(heading: str, content: str) -> bool:
    heading_lower = heading.lower().strip()
    content_lower = content.lower().strip()
    if heading_lower in {
        "table of contents",
        "contents",
        "sections",
        "содержание",
        "目录",
    }:
        return True
    if "cookie" in heading_lower or "файлы cookie" in heading_lower:
        return True
    hotkey_markers = ("hotkeys", "copy selection", "browser-based page search", "pgdn")
    if len(content) < 1000 and sum(marker in content_lower for marker in hotkey_markers) >= 2:
        return True
    return False

def _sections(
    tokens: list[tuple[str, str]], fallback_title: str
) -> tuple[tuple[str, str], ...]:
    sections: list[tuple[str, str]] = []
    heading = fallback_title
    paragraphs: list[str] = []
    for kind, value in tokens:
        if kind == "heading":
            if paragraphs:
                content = _trim_ui_noise("\n".join(paragraphs))
                if len(content) >= 100 and not _is_noise_section(heading, content):
                    sections.append((heading, content))
            heading = value
            paragraphs = []
        elif value != heading:
            paragraphs.append(value)
    if paragraphs:
        content = _trim_ui_noise("\n".join(paragraphs))
        if len(content) >= 100 and not _is_noise_section(heading, content):
            sections.append((heading, content))
    if not sections:
        text = _trim_ui_noise("\n".join(value for _, value in tokens))
        if len(text) >= 120 and not _is_noise_section(fallback_title, text):
            sections.append((fallback_title, text))
    return tuple(sections[:80])


def extract_document(body: bytes, content_type: str = "text/html") -> ExtractedDocument:
    if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
        raise ValueError(f"暂不支持的官方内容类型: {content_type}")
    decoded = _decode(body)
    if content_type == "text/plain":
        text = _clean(decoded)
        if len(text) < 120:
            raise ValueError("官方页面正文不足，可能是登录页或动态空壳")
        title = text[:100] or "Official document"
        return ExtractedDocument(
            title=title,
            language="",
            text=text,
            sections=((title, text),),
        )
    parser = VisibleTextParser()
    parser.feed(decoded)
    parser.close()
    title = _clean(" ".join(parser.title_parts))
    if not title:
        title = next(
            (value for kind, value in parser.tokens if kind == "heading"),
            "Official document",
        )
    text = "\n".join(value for _, value in parser.tokens)
    substantive = re.sub(r"\s+", " ", text).strip()
    non_document_titles = {
        "похоже, нет соединения",
        "просмотр статей",
        "no internet connection",
    }
    if title.lower().strip() in non_document_titles:
        raise ValueError("官方页面返回断网提示或动态阅读器空壳")
    error_markers = (
        "ошибка 404",
        "error 404",
        "page not found",
        "не можем найти нужную вам страницу",
        "похоже, нет соединения",
        "no internet connection",
    )
    if any(marker in substantive.lower() for marker in error_markers):
        raise ValueError("官方页面返回 404 或未找到页面")
    if len(substantive) < 120 or substantive == title:
        raise ValueError("官方页面正文不足，可能是登录页或动态空壳")
    published_at = _date_after(("published", "发布日期", "опубликовано"), text)
    effective_at = _date_after(
        ("effective", "生效", "вступает в силу", "действует с"), text
    )
    last_updated = _date_after(
        ("last updated", "updated", "最后更新", "обновлено"), text
    )
    sections = _sections(parser.tokens, title)
    if not sections:
        raise ValueError("官方页面没有可用的实质规则章节")
    return ExtractedDocument(
        title=title[:500],
        language=parser.language[:20],
        text=text,
        sections=sections,
        published_at=published_at or last_updated or _leading_date(text),
        effective_at=effective_at,
    )







