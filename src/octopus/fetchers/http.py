"""Shared blocking HTTP GET with error classification (fetchers run in threads)."""
from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from octopus.fetchers.base import FetchError, check_cancelled
from octopus.i18n import t
from octopus.media import AUDIO_SUFFIXES, VIDEO_SUFFIXES

DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
WECHAT_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.70(0x18004629) NetType/WIFI Language/zh_CN")
# Wikimedia answers 403 to browser-looking clients and welcomes descriptive bot UAs (their robot policy).
BOT_UA = "Octopus/0.1 (personal knowledge base; https://github.com/andrewman2035/octopus-kb)"
_BOT_UA_DOMAINS = ("wikipedia.org", "wikimedia.org", "wikidata.org")

MAX_PAGE_BYTES = 20_000_000  # all max_bytes parameters are bytes
_CONTENT_TYPE_SUFFIX = {
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/aiff": ".aiff", "audio/x-aiff": ".aiff",
    "video/mp4": ".mp4", "application/pdf": ".pdf",
}
_KNOWN_SUFFIXES = set(_CONTENT_TYPE_SUFFIX.values()) | set(AUDIO_SUFFIXES) | set(VIDEO_SUFFIXES)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if round(n / 1024) < 1024:  # not "1024 KB"
        return f"{n / 1024:.0f} KB"
    mib = n / 1_048_576
    return f"{mib:.1f} MB" if mib < 10 else f"{int(mib)} MB"


def user_agent_for(url: str, ua: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if any(host == d or host.endswith("." + d) for d in _BOT_UA_DOMAINS):
        return BOT_UA
    return ua


def _headers(ua: str, url: str) -> dict[str, str]:
    return {"User-Agent": user_agent_for(url, ua), "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}


def _check_status(r: httpx.Response) -> None:
    if r.is_redirect:  # only seen when redirects are not followed: the caller allows its first host only
        raise FetchError(t("fetch.refused", status=r.status_code), retryable=False)
    if r.status_code == 429 or r.status_code >= 500:
        raise FetchError(t("fetch.unavailable", status=r.status_code), retryable=True)
    if r.status_code >= 400:
        raise FetchError(t("fetch.refused", status=r.status_code), retryable=False)


def _network_error(e: Exception) -> FetchError:
    if isinstance(e, httpx.TransportError):
        return FetchError(t("fetch.network", error=e), retryable=True)
    return FetchError(t("fetch.request_failed", error=e), retryable=False)  # TooManyRedirects, DecodingError, bad URL...


def get(url: str, *, ua: str = DESKTOP_UA, timeout: float = 20.0, max_bytes: int = MAX_PAGE_BYTES,
        follow_redirects: bool = True) -> httpx.Response:
    try:
        with httpx.stream("GET", url, headers=_headers(ua, url), follow_redirects=follow_redirects,
                          timeout=timeout) as r:
            _check_status(r)
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise FetchError(t("fetch.too_large", size=_fmt_size(max_bytes)), retryable=False)
                chunks.append(chunk)
            # iter_bytes() already decoded gzip/br; drop encoding headers so the new Response doesn't decode twice
            headers = [(k, v) for k, v in r.headers.items()
                       if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
            return httpx.Response(r.status_code, headers=headers, content=b"".join(chunks), request=r.request,
                                  history=r.history)  # the redirects: links of this page too (P36)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise _network_error(e) from e


def resolve(url: str, *, ua: str = DESKTOP_UA, timeout: float = 20.0) -> str:
    """Follow redirects (short links) and return the final URL."""
    try:
        r = httpx.head(url, headers=_headers(ua, url), follow_redirects=True, timeout=timeout)
        if r.status_code in (403, 405, 501):  # server rejects HEAD: redirects are still followed by a GET
            with httpx.stream("GET", url, headers=_headers(ua, url), follow_redirects=True, timeout=timeout) as g:
                _check_status(g)
                return str(g.url)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise _network_error(e) from e
    _check_status(r)
    return str(r.url)


def download(url: str, dest_dir: Path, *, max_bytes: int, ua: str = DESKTOP_UA, timeout: float = 60.0,
             suffix: str | None = None, follow_redirects: bool = True) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path: Path | None = None
    try:
        with httpx.stream("GET", url, headers=_headers(ua, url), follow_redirects=follow_redirects,
                          timeout=timeout) as r:
            _check_status(r)
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            url_ext = Path(urlsplit(str(r.url)).path).suffix.lower()
            if suffix:
                ext = suffix
            elif url_ext in _KNOWN_SUFFIXES:
                ext = url_ext
            else:
                ext = _CONTENT_TYPE_SUFFIX.get(ctype) or (url_ext if 0 < len(url_ext) <= 6 else ".bin")
            path = dest_dir / f"{uuid.uuid4().hex}{ext}"
            size = 0
            try:
                with path.open("wb") as f:
                    for chunk in r.iter_bytes():
                        check_cancelled()
                        size += len(chunk)
                        if size > max_bytes:
                            raise FetchError(t("fetch.file_too_large", size=_fmt_size(max_bytes)), retryable=False)
                        f.write(chunk)
            except OSError as e:
                raise FetchError(t("fetch.save_failed", error=e), retryable=False) from e
        return path
    except BaseException as e:
        if path is not None:
            path.unlink(missing_ok=True)
        if isinstance(e, (httpx.HTTPError, httpx.InvalidURL)):
            raise _network_error(e) from e
        raise
