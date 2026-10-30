"""Generic article extraction with trafilatura (web pages, Substack, Medium, newsletters)."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import trafilatura

from octopus.fetchers import http
from octopus.fetchers.base import FetchError, FetchResult
from octopus.i18n import t
from octopus.urls import page_aliases

MIN_TEXT = 200
# a short page that asks to log in, pay or open the app: a wall, not the content (P17 fix rounds 2 and 3). English
# phrases as whole words ("blog in", "catalog includes" are not walls)
WALL_RE = re.compile(r"\blog\s?in\b|\bsign\s?(in|up)\b|\bcreate an account\b|\bsubscribe to (continue|read)\b|"
                     r"\bcontinue reading\b|登录|登陆|注册|扫码|打开\s*app|下载\s*app|开通会员|付费(阅读|内容)|订阅后",
                     re.I)  # zh: content
WALL_REST_CHARS = 40  # what a short page must still say without its wall sentences to count as content
_SENTENCES = re.compile(r"\n|(?<=[。！？!?])|(?<=\.)\s")  # zh: content


def looks_walled(text: str) -> bool:
    """Short text (under MIN_TEXT) that is mostly a wall: without the lines and sentences naming one, fewer than
    WALL_REST_CHARS characters (spaces aside) are left. A real post that mentions "log in" in one sentence is
    content."""
    if len(text) >= MIN_TEXT or not WALL_RE.search(text):
        return False
    rest = "".join(part for part in _SENTENCES.split(text) if not WALL_RE.search(part))
    return len("".join(rest.split())) < WALL_REST_CHARS


def classify_text(text: str, title: str | None) -> tuple[str, str | None]:
    if len(text) >= MIN_TEXT:
        return "ok", None
    if text or title:
        return "partial", t("fetch.short")
    return "needs_manual", t("fetch.no_body")


def page_link(html: str, url: str) -> str | None:
    """The page's own link as it names it (its canonical / og:url), normalized; None when it names none, a login /
    home page, or a page above url (a section, a blog's front page: shared by many pages, P36 fix round 2)."""
    meta = trafilatura.extract_metadata(html)
    link = next(iter(page_aliases(url, [meta.url])), None) if meta and meta.url else None
    if link is None:
        return None
    up, here = urlsplit(link), urlsplit(url)
    above = up.netloc == here.netloc and here.path.startswith(up.path.rstrip("/") + "/") and not up.query
    return None if above else link


def parse_html(url: str, html: str) -> FetchResult:
    """The article in a page's HTML, however it was got (fetched here, or sent by the Chrome extension)."""
    meta = trafilatura.extract_metadata(html, default_url=url)
    text = trafilatura.extract(html, url=url, output_format="markdown", include_comments=False,
                               include_tables=True, favor_recall=True) or ""
    title = meta.title if meta and meta.title else None
    author = meta.author if meta and meta.author else None
    if len(text) < MIN_TEXT and meta and meta.description and meta.description not in text:
        text = f"{meta.description}\n\n{text}".strip()
    status, note = classify_text(text, title)
    return FetchResult(url=url, source="web", title=title, author=author, text=text.strip(),
                       status=status, note=note)


class WebFetcher:
    name = "web"

    def can_handle(self, url: str) -> bool:
        return True

    def fetch(self, url: str) -> FetchResult:
        r = http.get(url)
        ctype = r.headers.get("content-type", "text/html").split(";")[0].strip().lower()
        if ctype and not (ctype.startswith("text/") or "html" in ctype or "xml" in ctype):
            raise FetchError(t("fetch.not_a_page", type=ctype), retryable=False)
        result = self.parse(url, r.text)
        result.aliases = [*(str(h.url) for h in r.history[1:]), str(r.url), *result.aliases]  # where it redirected
        return result

    def parse(self, url: str, html: str) -> FetchResult:
        return parse_html(url, html)
