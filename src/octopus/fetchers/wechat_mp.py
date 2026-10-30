"""WeChat Official Account articles (subscription and service accounts, mp.weixin.qq.com)."""
from __future__ import annotations

import html as htmllib
import logging
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, NavigableString
from bs4.element import PreformattedString

from octopus.fetchers import http
from octopus.fetchers.base import FetchError, FetchResult
from octopus.fetchers.web import MIN_TEXT, classify_text
from octopus.i18n import t
from octopus.urls import host_of, normalize_url

BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "blockquote", "section", "div",
          "table", "tr", "td", "th", "pre", "figure", "figcaption"}
SKIP = {"script", "style"}
log = logging.getLogger(__name__)

GONE_MARKERS = ("该内容已被发布者删除", "此内容因违规无法查看", "该公众号已迁移", "内容已被删除")  # zh: content
VERIFY_MARKERS = ("环境异常", "完成验证")  # zh: content
MAX_POST_IMAGES = 30
DEFAULT_MAX_BYTES = 20_000_000
_JS_ESCAPE = re.compile(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})|\\u\{([0-9a-fA-F]{1,6})\}|\\(.)", re.S)
# \b \f \v \0 are control characters in JS: dropped; a backslash before a line break continues the string
_JS_SIMPLE = {"n": "\n", "r": "", "t": "\t", "b": "", "f": "", "v": "", "0": "", "\n": "", "\r": ""}
# an item-level image url is the cdn_url directly followed by width:; watermark / share_cover ones are not
_PICTURE_URL = re.compile(r"cdn_url:\s*'((?:[^'\\]|\\.)*)'\s*,\s*width:")
_CAPTION = re.compile(r"content_noencode:\s*'((?:[^'\\]|\\.)*)'", re.S)
_SHARE_TAG = re.compile(r"<[^<>]*\bid\s*=\s*[\"']?js_share_source\b[^<>]*>", re.I)  # no "<": linear
_DATA_URL = re.compile(r"\bdata-url\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I)
_NICK_NAME = re.compile(r"nick_name:\s*'((?:[^'\\]|\\.)*)'")
_IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif", "image/webp": "webp"}
_FMT = {"png": "png", "jpeg": "jpg", "jpg": "jpg", "gif": "gif", "webp": "webp"}


def _is_cjk(ch: str) -> bool:
    # CJK ideographs plus CJK / fullwidth punctuation and curly quotes
    return ("\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f"
            or "\uff00" <= ch <= "\uffef" or "\u2018" <= ch <= "\u201d")


def _join_lines(parts: list[str]) -> str:
    out = ""
    for part in parts:
        if out and part:
            out += "" if _is_cjk(out[-1]) and _is_cjk(part[0]) else " "
        out += part
    return out


_WS = re.compile(r"[ \t\u00a0]+")


def _paragraph(buf: list[str]) -> str:
    lines = (_WS.sub(" ", ln).strip() for ln in "".join(buf).split("\n"))
    return _join_lines([ln for ln in lines if ln])


def _body(content) -> str:
    """Single DOM pass: inline text accumulates in a buffer that is flushed at every block boundary,
    so each text node is emitted exactly once (no loss, no duplication)."""
    paras: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if (para := _paragraph(buf)):
            paras.append(para)
        buf.clear()

    def walk(node) -> None:
        for child in node.children:
            if isinstance(child, PreformattedString):  # comments, the doctype, CDATA…
                continue
            if isinstance(child, NavigableString):
                buf.append(str(child))
            elif child.name in SKIP:
                continue
            elif child.name == "br":
                buf.append("\n")
            elif child.name in BLOCKS:
                flush()
                walk(child)
                flush()
            else:
                walk(child)

    walk(content)
    flush()
    return "\n\n".join(paras)


def _js_decode(raw: str) -> str:
    """A single-quoted JS string literal's body, decoded by hand (never evaluated)."""
    def sub(m: re.Match) -> str:
        if (code := m.group(1) or m.group(2) or m.group(3)) is not None:
            n = int(code, 16)
            return chr(n) if n <= 0x10FFFF else "\ufffd"
        return _JS_SIMPLE.get(m.group(4), m.group(4))

    text = _JS_ESCAPE.sub(sub, raw)
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")  # \ud83d\ude00 -> one character


def _js_unescape(raw: str) -> str:
    return htmllib.unescape(_js_decode(raw))


def _inline_article(html: str) -> str:
    """A shared page's article ("share an article": #js_content is empty): the HTML in its content_noencode string."""
    texts = [_body(BeautifulSoup(_js_decode(raw), "lxml")) for raw in _CAPTION.findall(html)]
    return max(texts, key=len, default="")


def _share_source(html: str) -> str | None:
    """The original article a shared page links to: an https mp.weixin.qq.com link, or None."""
    tag = _SHARE_TAG.search(html)
    m = _DATA_URL.search(tag.group(0)) if tag else None
    parts = urlsplit(htmllib.unescape(m.group(1) or m.group(2) or "").strip()) if m else None
    if parts is None or parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != "mp.weixin.qq.com":
        return None
    return normalize_url(parts._replace(scheme="https", netloc="mp.weixin.qq.com").geturl(), trim=False)


def _image_ext(url: str, content_type: str) -> str:
    fmt = (parse_qs(urlsplit(url).query).get("wx_fmt") or [""])[0].lower()
    return _FMT.get(fmt) or _IMAGE_TYPES.get(content_type.split(";")[0].strip().lower()) or "jpg"


class WeChatMPFetcher:
    name = "wechat_mp"

    def __init__(self, downloads_dir: Path | None = None, max_bytes: int = DEFAULT_MAX_BYTES):
        self.downloads_dir = downloads_dir
        self.max_bytes = max_bytes

    def can_handle(self, url: str) -> bool:
        return (urlsplit(url).hostname or "").lower() == "mp.weixin.qq.com"

    def fetch(self, url: str) -> FetchResult:
        html = http.get(url, ua=http.WECHAT_UA).text
        result = self.parse(url, html)
        if (result.status == "ok" or result.image_paths or not (source := _share_source(html))
                or source == normalize_url(url, trim=False)):
            return result
        # a shared page whose article is not inline: read the original it links to, one hop. Whatever happens there,
        # the result is never worse than the shared page alone (saved now, no retry)
        try:
            original = self.parse(source, http.get(source, ua=http.WECHAT_UA).text)
        except Exception as e:
            reason = str(e) if isinstance(e, FetchError) else t("fetch.unexpected")
            if reason == t("fetch.wechat_verify"):
                reason = t("fetch.wechat_verify_short")  # nothing retries the original: no "trying again later"
            log.info("wechat: the shared article's original was not read (%s)", type(e).__name__)  # no link (R4)
            return self._original_missed(result, reason)
        if len(original.text) <= len(result.text) and not original.image_paths:
            return self._original_missed(result, original.note or t("fetch.short"))
        original.text = "\n\n".join(p for p in (t("fetch.wechat_original", url=source), original.text) if p)
        original.url = url
        original.title = original.title or result.title
        original.author = original.author or result.author
        return original

    @staticmethod
    def _original_missed(result: FetchResult, reason: str) -> FetchResult:
        missed = t("fetch.wechat_original_missed", reason=reason)
        result.note = t("common.note_sep").join(n for n in (result.note, missed) if n)
        return result

    def parse(self, url: str, html: str) -> FetchResult:
        """An article page's HTML, fetched here or sent by the Chrome extension."""
        soup = BeautifulSoup(html, "lxml")
        content = soup.find(id="js_content")
        if content is None:
            urls = list(dict.fromkeys(_js_unescape(u) for u in _PICTURE_URL.findall(
                html[html.find("picture_page_info_list"):] if "picture_page_info_list" in html else "")))
            if urls:
                return self._picture_post(url, html, soup, urls)
            if any(m in html for m in GONE_MARKERS):
                return FetchResult(url=url, source=self.name, status="needs_manual", note=t("fetch.wechat_gone"))
            if any(m in html for m in VERIFY_MARKERS):
                raise FetchError(t("fetch.wechat_verify"), retryable=True)
            # often an anti-bot page in a new wording, gone a few minutes later: retry, and log what came back
            page_title = soup.title.get_text(strip=True) if soup.title else ""
            # the host only (R4: no full links in the log); the job's own line after this says which job it was
            log.warning("wechat: no article body at %s (page title %r, %d chars)", host_of(url), page_title, len(html))
            raise FetchError(t("fetch.wechat_no_body"), retryable=True)
        og = soup.find("meta", attrs={"property": "og:title"})
        h1 = soup.find(id="activity-name")
        title = (og.get("content") if og else None) or (h1.get_text(strip=True) if h1 else None)
        name = soup.find(id="js_name")
        meta_author = soup.find("meta", attrs={"name": "author"})
        author = (name.get_text(strip=True) if name else None) or (meta_author.get("content") if meta_author else None)
        text = _body(content)
        if len(text) < MIN_TEXT:
            try:
                inline = _inline_article(html)
            except Exception as e:  # a secondary source: it never fails the page
                log.info("wechat: inline article at %s not read (%s)", host_of(url), type(e).__name__)
                inline = ""
            if len(inline) > len(text):
                text = inline
        status, note = classify_text(text, title)
        return FetchResult(url=url, source=self.name, title=title, author=author, text=text,
                           status=status, note=note)

    def _picture_post(self, url: str, html: str, soup: BeautifulSoup, urls: list[str]) -> FetchResult:
        m = _CAPTION.search(html)
        caption = _js_unescape(m.group(1)).strip() if m else ""
        og = soup.find("meta", attrs={"property": "og:title"})
        h1 = soup.find(id="activity-name")
        title = ((og.get("content") if og else None) or (h1.get_text(strip=True) if h1 else None)
                 or (caption.splitlines()[0] if caption else None))
        name = soup.find(id="js_name")
        nick = _NICK_NAME.search(html)
        author = (name.get_text(strip=True) if name else None) or (_js_unescape(nick.group(1)) if nick else None)
        notes = []
        if len(urls) > MAX_POST_IMAGES:
            urls = urls[:MAX_POST_IMAGES]
            notes.append(t("fetch.too_many_images", n=MAX_POST_IMAGES))
        if self.downloads_dir is None:
            return FetchResult(url=url, source=self.name, title=title, author=author, text=caption,
                               status="partial", note=t("fetch.image_post_no_ocr"))
        folder = self.downloads_dir / f"wechat-{uuid.uuid4().hex}"
        paths, failed = [], 0
        for image_url in urls:
            try:
                r = http.get(image_url, ua=http.WECHAT_UA, max_bytes=self.max_bytes)
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{len(paths) + 1:02d}.{_image_ext(image_url, r.headers.get('content-type', ''))}"
                path.write_bytes(r.content)
            except (FetchError, OSError):
                failed += 1
                continue
            paths.append(path)
        status = "ok"
        if not paths:
            if not caption:
                raise FetchError(t("fetch.images_failed"), retryable=True)
            return FetchResult(url=url, source=self.name, title=title, author=author, text=caption,
                               status="partial", note=t("common.note_sep").join([*notes, t("fetch.images_failed")]))
        if failed:
            status = "partial"
            notes.append(t("fetch.some_images_failed", n=failed))
        return FetchResult(url=url, source=self.name, title=title, author=author, text=caption,
                           image_paths=paths, status=status, note=t("common.note_sep").join(notes) or None)
