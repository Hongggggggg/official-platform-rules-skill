from __future__ import annotations

import gzip
import http.client
import http.cookiejar
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .models import FetchResult, utc_now


class FetchError(RuntimeError):
    pass


MAX_BODY_BYTES = 8 * 1024 * 1024
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _open(request: urllib.request.Request, timeout: int):
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )
    return opener.open(request, timeout=timeout)


def fetch_url(
    url: str,
    timeout: int = 30,
    attempts: int = 3,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; OfficialPlatformRules/0.3; "
            "official-policy-monitor; no authentication bypass)"
        ),
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-US,en;q=0.8",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            with _open(request, timeout) as response:
                body = response.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise FetchError("官方页面超过 8 MiB 安全限制")
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    body = gzip.decompress(body)
                return FetchResult(
                    url=response.geturl(),
                    status=int(response.status),
                    content_type=response.headers.get_content_type(),
                    body=body,
                    fetched_at=utc_now(),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return FetchResult(
                    url=exc.geturl(),
                    status=304,
                    content_type=exc.headers.get_content_type(),
                    body=b"",
                    fetched_at=utc_now(),
                    etag=exc.headers.get("ETag") or etag,
                    last_modified=(
                        exc.headers.get("Last-Modified") or last_modified
                    ),
                )
            last_error = exc
            if exc.code not in RETRYABLE_STATUS or attempt + 1 >= attempts:
                break
        except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
        time.sleep(0.4 * (attempt + 1))
    raise FetchError(f"无法获取官方页面: {last_error}") from last_error

def fetch_rendered_url(url: str, timeout: int = 30) -> FetchResult:
    candidates = (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    )
    browser = next((path for path in candidates if path.is_file()), None)
    if browser is None:
        raise FetchError("动态官方页面需要本机 Edge 或 Chrome，当前未找到")
    budget = max(5000, min(timeout * 1000, 30000))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    diagnostics: list[str] = []
    headless_modes = ("--headless=new", "--headless")
    for headless_mode in headless_modes:
        try:
            with tempfile.TemporaryDirectory(prefix="official-rules-render-") as profile:
                profile_path = Path(profile)
                completed = subprocess.run(
                    [
                        str(browser),
                        headless_mode,
                        "--disable-gpu",
                        "--disable-gpu-compositing",
                        "--disable-software-rasterizer",
                        "--in-process-gpu",
                        "--no-sandbox",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--disable-breakpad",
                        "--disable-features=Vulkan,UseDawn,SkiaGraphite,UseSkiaRenderer",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--hide-scrollbars",
                        "--renderer-process-limit=1",
                        f"--user-data-dir={profile}",
                        f"--disk-cache-dir={profile_path / 'cache'}",
                        f"--virtual-time-budget={budget}",
                        "--dump-dom",
                        url,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout + 15,
                    check=False,
                    creationflags=flags,
                )
        except (subprocess.SubprocessError, OSError) as exc:
            diagnostics.append(f"{headless_mode}: {exc}")
            continue
        if completed.returncode == 0 and len(completed.stdout) >= 120:
            return FetchResult(
                url=url,
                status=200,
                content_type="text/html",
                body=completed.stdout,
                fetched_at=utc_now(),
            )
        detail = completed.stderr.decode("utf-8", errors="replace")[-500:]
        diagnostics.append(
            f"{headless_mode}: returncode={completed.returncode}; {detail}"
        )
    raise FetchError(
        "动态官方页面未返回正文: " + " || ".join(diagnostics)[-1200:]
    )

