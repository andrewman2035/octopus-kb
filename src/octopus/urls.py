"""URL extraction and normalization (tracking parameters removed, used for dedupe)."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Curly quotes, … and CJK / full-width punctuation (the ideographic comma and full stop, corner, angle and
# lenticular brackets, full-width brackets, comma, colon, "!" and "?" …) never belong to a link: a link followed by a
# Chinese colon or a curly quote, or inside full-width parentheses, ends there. Punctuation only: the wave dashes,
# full-width letters and digits, half-width katakana stay in it
_NOT_IN_LINK = ("\u2018-\u201f\u2026\u3000-\u3004\u3008-\u3011\u3014-\u301b\u301d-\u301f"
                "\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff5d\uff5f-\uff65")
URL_RE = re.compile(rf"https?://[^\s<>\"'`{_NOT_IN_LINK}]+", re.I)
TRAILING = ".,;:!?)]}'\"）】」》，。！？；：’‘”“』〉〕］｝…"  # zh: content

GENERIC_TRACKING = {
    "spm", "spm_id_from", "fbclid", "gclid", "igsh", "igshid", "vd_source", "si", "feature",
    "ref_src", "xsec_source", "from_source", "timestamp", "unique_k", "mpshare",
}
TRACKING_PREFIXES = ("utm_", "share_", "sharer_")
HOST_TRACKING = {
    "mp.weixin.qq.com": {
        "chksm", "scene", "subscene", "sessionid", "clicktime", "enterid", "srcid", "exportkey",
        "pass_ticket", "wx_header", "ascene", "devicetype", "version", "nettype", "lang",
        "abtest_cookie", "from", "poc_token",
    },
    "x.com": {"s", "t"},
    "twitter.com": {"s", "t"},
}


# A link typed without https:// ("mp.weixin.qq.com/s/…"): a dotted host, then a non-empty path. Not glued to
# a letter, digit, @ or / before it, so file paths, emails and the host inside a full link never match. The
# path stops at Chinese text: share blurbs glue their "copy and open" words straight onto the link.
BARE_URL_RE = re.compile(
    rf"(?<![A-Za-z0-9@/._-])((?:[A-Za-z0-9-]+\.)+([A-Za-z]{{2,}}))/[^\s<>\"'`{_NOT_IN_LINK}\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]+")
# Inside other text a bare link counts only on these hosts (and their subdomains): "python.org/downloads"
# in a question stays a question. A message that is nothing but the bare link counts on any host with a
# common lowercase top-level domain ("Node.js/Express", "ASP.NET/Core" or "foo.py/bar" alone is not a link).
BARE_HOSTS = (
    "weixin.qq.com", "b23.tv", "bilibili.com", "youtu.be", "youtube.com", "x.com", "twitter.com",
    "xiaohongshu.com", "xhslink.com", "xhslink.cn", "douyin.com", "reddit.com", "redd.it", "zhihu.com", "github.com",
    "xiaoyuzhoufm.com", "podcasts.apple.com", "weibo.com", "medium.com", "substack.com",
)
COMMON_TLDS = {
    "com", "net", "org", "cn", "io", "co", "tv", "be", "me", "ai", "app", "dev", "xyz", "cc", "top", "info",
    "site", "so", "ly", "gl", "fm", "link", "news", "blog", "page", "tech", "edu", "gov", "us", "uk", "jp",
    "hk", "tw", "sg", "de", "fr", "ca", "au", "in", "it",
}


def _bare_ok(host: str, tld: str, whole: bool) -> bool:
    host = host.lower()
    return (whole and tld in COMMON_TLDS) or any(host == h or host.endswith("." + h) for h in BARE_HOSTS)


def _trim(raw: str) -> str:
    """The link without the punctuation after it; a ")" or "]" closing one opened inside the link stays (Wikipedia
    titles, an IPv6 host)."""
    url = raw.rstrip(TRAILING)
    while len(url) < len(raw) and any(raw[len(url)] == c and url.count(o) > url.count(c) for o, c in ("()", "[]")):
        url += raw[len(url)]
    return url


def parsable(url: str) -> bool:
    """urllib can read the link and it names a host: "http://[abc/def" or "http://:80/x" is just text."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 -- raises on a port out of range or not a number
    except ValueError:
        return False
    return bool(parts.hostname)


def _links(text: str, parsable_only: bool = True) -> list[tuple[int, int, str]]:
    """(start, end, url) of each link as typed, in order; a bare one's url gets https://."""
    found = []
    for m in URL_RE.finditer(text):
        url = _trim(m.group(0))
        if not parsable_only or parsable(url):
            found.append((m.start(), m.start() + len(url), url))
    rest = URL_RE.sub(lambda m: " " * len(m.group(0)), text)  # full links blanked: their hosts are not bare
    whole = len(rest.split()) == 1 and rest == text  # no full link, not even one urllib cannot parse
    for m in BARE_URL_RE.finditer(rest):
        url = _trim(m.group(0))
        if (not url.endswith("/") and _bare_ok(m.group(1), m.group(2), whole)  # "x.com/" alone has no path
                and (not parsable_only or parsable("https://" + url))):
            found.append((m.start(), m.start() + len(url), "https://" + url))
    return sorted(found)


def extract_urls(text: str) -> list[str]:
    return list(dict.fromkeys(u for _, _, u in _links(text) if u))


def without_urls(text: str) -> str:
    """The text with the links extract_urls finds in it taken out."""
    out, pos = [], 0
    for start, end, _ in _links(text):
        out.append(text[pos:start])
        pos = end
    return "".join(out) + text[pos:]


def host_of(url: str | None) -> str:
    """The link's host for a log line ("" when there is none); never raises."""
    try:
        return urlsplit(url or "").hostname or ""
    except ValueError:  # "http://[server IP]:8080/…", a placeholder in pasted tutorials: not a valid IPv6 host
        return ""


def links_as_hosts(text: str) -> str:
    """The text with each link extract_urls finds in it shown as the log's "link <host>" (its wording
    is fixed in Chinese): for logs, no paths or queries.

    Also one urllib cannot parse (no link to save, but its path stays out of the log all the same)."""
    out, pos = [], 0
    for start, end, url in _links(text, parsable_only=False):
        host = host_of(url)
        out.append(f"{text[pos:start]}链接 {host}" if host else f"{text[pos:start]}链接")  # zh: log
        pos = end
    return "".join(out) + text[pos:]


def normalize_url(url: str, trim: bool = True) -> str:
    """The link without tracking parameters; one urllib cannot parse comes back as given (stripped). trim: drop the
    punctuation after a link typed in a sentence (not for a browser tab's exact link)."""
    url = _trim(url.strip()) if trim else url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.netloc.lower()
    drop = GENERIC_TRACKING | HOST_TRACKING.get(host, set())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in drop and not k.startswith(TRACKING_PREFIXES)
    ]
    return urlunsplit((parts.scheme.lower(), host, parts.path, urlencode(query, safe=":/"), ""))


# P36 fix round 1 (I-2): one post's links as the phone app shares them and as the browser shows them, made one key.
# Only where the post id is in both: Xiaohongshu (/explore/<id>, /discovery/item/<id>), Reddit (/comments/<id>,
# redd.it/<id>), Threads (threads.net moved to threads.com), Weibo (the same post id after weibo.com/<uid>/ and
# m.weibo.cn/status/). A Reddit app link (/r/<sub>/s/<code>) carries no post id: it is matched only through the
# address it redirects to, and Reddit is never fetched (README says so).
_XHS_RE = re.compile(r"^/(?:explore|discovery/item)/([0-9a-f]{24})(?:/|$)")
_REDDIT_RE = re.compile(r"^(?:/r/[^/]+)?/comments/([a-z0-9]+)(?:/|$)", re.I)
# a post's id: a bid (9 or more letters and digits, a digit among them) or a numeric mid (15 or more digits); a
# profile page's word (weibo.com/<uid>/profile) is none
_WEIBO_RE = re.compile(r"^/(?:status|detail|\d+)/((?=[A-Za-z]*\d)[A-Za-z0-9]{9,}|\d{15,})/?$")
# a redirect or canonical that is not the post: a login, verification or error page, or a site's home page
_NOT_THE_PAGE = re.compile(r"log-?in|sign-?(?:in|up)|captcha|passport|verif|/404\b|/error\b", re.I)


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def same_page(url: str) -> str | None:
    """The key the platform's post has whatever link form it was saved under; None for any other link."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host, path = (parts.hostname or "").rstrip("."), parts.path
    if _on(host, "xiaohongshu.com") and (m := _XHS_RE.match(path)):
        return f"https://www.xiaohongshu.com/explore/{m.group(1)}"
    if _on(host, "reddit.com") and (m := _REDDIT_RE.match(path)):
        return f"https://www.reddit.com/comments/{m.group(1).lower()}"
    if host == "redd.it" and re.fullmatch(r"/[a-z0-9]+/?", path, re.I):
        return f"https://www.reddit.com/comments/{path.strip('/').lower()}"
    if (_on(host, "threads.net") or _on(host, "threads.com")) and path.strip("/"):
        return f"https://www.threads.com{path.rstrip('/')}"
    if (_on(host, "weibo.com") or _on(host, "weibo.cn")) and (m := _WEIBO_RE.match(path)):
        return f"weibo:{m.group(1)}"
    return None


def page_aliases(url: str | None, others) -> list[str]:
    """The other links a page was reached by (redirects, its canonical / og:url, the browser tab's), normalized,
    without url itself and without login, error and home pages."""
    own = normalize_url(url, trim=False) if url else None
    out: list[str] = []
    for raw in others:
        if not raw or not parsable(raw):
            continue
        alias = normalize_url(raw, trim=False)
        parts = urlsplit(alias)
        if (parts.scheme in ("http", "https") and parts.path.strip("/") and alias != own and alias not in out
                and not _NOT_THE_PAGE.search((parts.hostname or "") + parts.path)):
            out.append(alias)
    return out


def lookup_keys(url: str) -> list[str]:
    """What a link is looked up by: itself and its platform key."""
    key = same_page(url)
    return [url, key] if key and key != url else [url]
