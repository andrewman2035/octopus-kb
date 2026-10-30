"""Saves sent by email (P23): one dedicated mailbox, read over IMAP by channels/email.py; save-only, never chat.

A mail is taken only when its From is one of owner_addresses and the receiving provider's Authentication-Results header
(the topmost one: the provider adds its own on top of whatever the sender wrote) says dkim, spf or dmarc passed for
that address's domain. What is in it decides how it is saved (plan_mail): a mail that is little more than links saves
the links; a forwarded newsletter or HTML mail is read as an article from its HTML (the extension's page path); other
text is a note; attachments go the chat's attachment path. Each mail is its own user (mail-<row id>) of channel email:
the jobs' replies come back through EmailChannel.send to MailIntake.reply, which sends one reply per mail when all its
saves are done (by email, to Telegram, or nowhere)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from octopus.app import App
from octopus.channels.base import SendResult, log_preview, out_line, safe_filename, user_tag
from octopus.config import EmailConfig
from octopus.db import transaction
from octopus.formatting import format_duplicate
from octopus.i18n import aliases, t
from octopus.models import Attachment, attachment_to_dict
from octopus.urls import extract_urls, normalize_url, parsable, without_urls

log = logging.getLogger(__name__)
EMAIL = "email"
LINKS, ARTICLE, NOTE, FILES = "links", "article", "note", "files"
LINK_ONLY_CHARS = 100  # a mail with links and less than this much else saves the links ("Sent from my iPhone" …)
MAX_LINKS = 10
NEWSLETTER_LINKS = 3  # an HTML part with this many links is a newsletter, not typed words
KEEP_ROWS = 1000
_FORWARD_MARK = re.compile(r"^\s*-{2,}\s*(?:Forwarded message|Original Message|转发的邮件|原始邮件|原始郵件)\s*-{2,}\s*$",
                           re.I | re.M)  # zh: content
_HEADER_LINE = re.compile(r"^\s*(?:From|Date|Sent|Subject|To|Cc|发件人|发送时间|时间|日期|主题|收件人|抄送)\s*[:：].*$",
                          re.I | re.M)  # zh: content
_FROM_LINE = re.compile(r"^\s*(?:From|发件人)\s*[:：]\s*(.+)$", re.I | re.M)  # zh: content
_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:fwd?|fw|re|转发|回复)\s*[:：]\s*)+", re.I)  # zh: content
_VIEW_ONLINE = re.compile(r"view (?:this |the )?(?:e-?mail |post |issue |newsletter |it )?(?:in|on) (?:your |a |the )?"
                          r"(?:browser|web)|view online|web version|read (?:it )?online|online version|"
                          r"在浏览器中?(?:查看|打开|阅读)|网页版|在线阅读|在线查看|阅读原文", re.I)  # zh: content
_UNSUBSCRIBE = re.compile(r"unsubscribe|opt[ -]?out|manage (?:your )?(?:preferences|subscription)|email preferences|"
                          r"退订|取消订阅", re.I)  # zh: content
_UNSUB_PATH = re.compile(r"unsub|optout|opt-out|/track(?:ing)?/|e?mail-?preferences|manage-?preferences", re.I)
_BRACKETED = re.compile(r"<([^>]+)>")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
MAX_AR_CHARS = 8192  # one Authentication-Results header; Gmail's are well under 1 KB
MAX_AR_TOKENS = 2000
MAX_FORWARD_DEPTH = 3  # a forward attached inside a forward …
# the authserv-id each provider writes in its own Authentication-Results header (C2). Only names known for sure:
# Outlook / Hotmail write theirs with no authserv-id at all, so they cannot be told from a sender's and are not here
AUTHSERV_DEFAULTS = {"imap.gmail.com": ["mx.google.com"], "imap.googlemail.com": ["mx.google.com"]}
# relaxed alignment never goes up to one of these (a small list, not the Public Suffix List)
PUBLIC_SUFFIXES = {
    "com", "net", "org", "edu", "gov", "mil", "int", "info", "biz", "io", "co", "me", "ai", "app", "dev", "xyz",
    "cn", "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "hk", "com.hk", "org.hk", "tw", "com.tw", "org.tw", "mo", "com.mo", "sg", "com.sg",
    "jp", "co.jp", "ne.jp", "or.jp", "ac.jp", "kr", "co.kr", "or.kr",
    "uk", "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk",
    "au", "com.au", "net.au", "org.au", "edu.au", "nz", "co.nz", "ca", "us", "de", "fr", "nl", "eu", "ch", "se",
    "it", "es", "ru", "in", "co.in", "br", "com.br",
    "github.io", "gitlab.io", "herokuapp.com", "appspot.com", "blogspot.com", "substack.com", "netlify.app",
    "vercel.app", "pages.dev", "workers.dev",
}
SKIPPED_SUBJECT_CHARS = 20
REPLY_SUBJECT = "Re: {subject}"
# what the reply mail says: short and plain
__getattr__ = aliases({"UNKNOWN_SERVER": "mail.unknown_server", "NOTHING_TO_SAVE": "mail.nothing_to_save",
                       "SAVE_ERROR": "mail.save_error", "SKIPPED": "mail.skipped", "NO_SUBJECT": "mail.no_subject",
                       "REPLY_SUBJECT_NONE": "mail.reply_subject_none"})


@dataclass
class MailFile:
    filename: str
    mime: str
    data: bytes


@dataclass
class Mail:
    sender: str  # the From address, lower case
    sender_name: str
    subject: str  # without Fwd: (or its Chinese form)
    message_id: str | None
    text: str  # the plain part, or the HTML part's text when there is none
    html: str | None
    files: list[MailFile] = field(default_factory=list)
    forwarded: bool = False
    original_sender: str | None = None  # a forwarded mail's From (name, else address)
    html_only: bool = False
    anchors: int = 0  # links in the HTML part (a newsletter has many)
    view_url: str | None = None  # the HTML's "view in browser" link
    skip_links: set[str] = field(default_factory=set)  # unsubscribe links: never fetched


@dataclass
class Plan:
    kind: str | None  # LINKS / ARTICLE / NOTE / FILES, None: nothing to save
    title: str = ""
    links: list[str] = field(default_factory=list)
    text: str = ""
    url: str | None = None  # an article's "view in browser" link
    links_unread: bool = False
    files: list[MailFile] = field(default_factory=list)
    caption: str = ""


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().strip(">").lower().rstrip(".")


def _aligned(signer: str, wanted: str) -> bool:
    """Relaxed alignment without the Public Suffix List: the same domain, or the From a subdomain of the signer's —
    never a public suffix (PUBLIC_SUFFIXES, or a name with no dot), never the signer a subdomain of the From."""
    signer = signer.lower().rstrip(".")
    if not signer or "." not in signer or signer in PUBLIC_SUFFIXES:
        return False
    return wanted == signer or wanted.endswith("." + signer)


def trusted_servers(conf: EmailConfig) -> list[str]:
    """The authserv-ids whose Authentication-Results header is the receiving provider's: config, else built in."""
    if conf.authserv_id:
        return [s.strip().lower() for s in conf.authserv_id if s.strip()]
    return list(AUTHSERV_DEFAULTS.get(conf.imap_host.strip().lower(), []))


@dataclass
class AuthResult:
    method: str
    result: str
    props: dict[str, str] = field(default_factory=dict)  # unquoted values only
    quoted: bool = False  # some property value was a quoted string: the result is not counted


def _ar_tokens(text: str) -> list[tuple[str, str]]:
    """RFC 8601 / 5322 lexing: ("word", text) with ("quoted", text) for a quoted string, ("=", "="), (";", ";") and
    (" ", " ") for a space between words; comments (nested, with \\ escapes) are dropped. Every step takes at least one
    character (a ")" that closes no comment is part of a word), and it stops after MAX_AR_TOKENS + 1 tokens."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n and len(out) <= MAX_AR_TOKENS:
        c = text[i]
        if c == "(":
            depth = 0
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                depth += {"(": 1, ")": -1}.get(text[i], 0)
                i += 1
                if depth == 0:
                    break
            token = (" ", " ")
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                buf.append(text[i])
                i += 1
            i += 1
            token = ("quoted", "".join(buf))
        elif c in ";=":
            token = (c, c)
            i += 1
        elif c.isspace():
            token = (" ", " ")
            i += 1
        else:
            j = i + 1  # at least this character, even a stray ")"
            while j < n and not text[j].isspace() and text[j] not in '(;="':
                j += 1
            token = ("word", text[i:j])
            i = j
        if token != (" ", " ") or not out or out[-1] != token:  # a run of spaces is one
            out.append(token)
    return out


def parse_auth_results(value: str) -> tuple[str | None, list[AuthResult]]:
    """An Authentication-Results header: (its authserv-id, or None when it has none, as Outlook's; the method results).
    A word made of a quoted string and more ("x"@evil.test) is one value, marked quoted. ValueError when the header is
    longer than MAX_AR_CHARS or has more than MAX_AR_TOKENS tokens (no provider writes one that big)."""
    if len(value) > MAX_AR_CHARS:
        raise ValueError("Authentication-Results too long")
    tokens = _ar_tokens(value)
    if len(tokens) > MAX_AR_TOKENS:
        raise ValueError("Authentication-Results has too many parts")
    segments: list[list[tuple[str, str]]] = [[]]
    for kind, text in tokens:
        if kind == ";":
            segments.append([])
        else:
            segments[-1].append((kind, text))

    def words(segment) -> list[tuple[str, bool] | str]:
        items: list = []
        joined = False  # the last item is a word the next word token continues
        for kind, text in segment:
            if kind in ("word", "quoted"):
                if joined:
                    items[-1] = (items[-1][0] + text, items[-1][1] or kind == "quoted")
                else:
                    items.append((text, kind == "quoted"))
                joined = True
            else:
                joined = False
                if kind == "=":
                    items.append("=")
        return items

    head = words(segments[0])
    server = head[0][0].lower() if head and "=" not in head and isinstance(head[0], tuple) else None
    results = []
    for segment in segments[1:] if server is not None else segments:
        items = words(segment)
        if len(items) < 3 or items[1] != "=" or not isinstance(items[0], tuple) or not isinstance(items[2], tuple):
            continue
        r = AuthResult(items[0][0].split("/")[0].lower(), items[2][0].lower(), quoted=items[0][1] or items[2][1])
        k = 3
        while k < len(items):
            name, eq, val = items[k], items[k + 1] if k + 1 < len(items) else None, \
                items[k + 2] if k + 2 < len(items) else None
            if isinstance(name, tuple) and eq == "=" and isinstance(val, tuple):
                if name[1] or val[1]:
                    r.quoted = True
                else:
                    r.props[name[0].lower()] = val[0]
                k += 3
            else:
                k += 1
        results.append(r)
    return server, results


def _plain_header(value: str) -> bool:
    """The provider's own header as Gmail writes it: no quotes, no backslashes, parentheses that pair up. Anything else
    may be a sender's text copied into it (a quoted envelope sender holding ") ; dkim=pass …"): not trusted (Q3)."""
    if '"' in value or "\\" in value:
        return False
    depth = 0
    for c in value:
        depth += {"(": 1, ")": -1}.get(c, 0)
        if depth < 0:
            return False
    return depth == 0


def authenticated(msg: EmailMessage, address: str, trusted: list[str]) -> bool:
    """The receiving provider's Authentication-Results says the mail passed for address's domain. The provider's
    header is the topmost one whose authserv-id is trusted (trusted_servers); every other header (one the sender wrote,
    wherever it sits and whatever it claims) is never read. A result with a quoted value is not counted.
    With a dmarc result in it, exactly one dmarc=pass whose header.from is address's domain is needed (more than one
    dmarc result: untrusted); only with no dmarc result at all, or one dmarc=temperror (the provider could not look
    the record up: P25), does an aligned dkim or spf pass count."""
    servers = {x.strip().lower() for x in trusted if x.strip()}
    wanted = _domain(address)
    for value in msg.get_all("Authentication-Results") or []:
        try:
            server, results = parse_auth_results(str(value))
        except Exception:  # too long, too many parts, anything: never authenticated, never an error
            return False
        if server is None or server not in servers:
            continue
        if not _plain_header(str(value)):
            log.info("email: the provider's Authentication-Results holds quotes, backslashes or unbalanced "
                     "parentheses; not trusted")
            return False
        dmarc = [r for r in results if r.method == "dmarc"]
        if dmarc:  # the provider's verdict on the From domain decides; a second one was put there by someone else
            if len(dmarc) > 1:
                log.info("email: the provider's Authentication-Results holds more than one dmarc result; not trusted")
                return False
            r = dmarc[0]
            if not _temporary(r, wanted):
                return r.result == "pass" and not r.quoted and r.props.get("header.from", "").lower().rstrip(".") == wanted
        for r in results:  # no dmarc verdict (no DMARC record, or the provider could not read it): dkim / spf alignment
            if r.result != "pass" or r.quoted:
                continue
            if r.method == "dkim":
                found = r.props.get("header.d") or _domain(r.props.get("header.i", ""))
            elif r.method == "spf":
                found = _domain(r.props.get("smtp.mailfrom", ""))
            else:
                continue
            if _aligned(found, wanted):
                return True
        return False  # only the topmost header of the provider
    return False


def _temporary(r, wanted: str) -> bool:
    """A dmarc=temperror result for the From domain: the provider could not look its DMARC record up (a passing DNS
    trouble on its side), not a verdict."""
    return (r.result == "temperror" and not r.quoted
            and r.props.get("header.from", "").lower().rstrip(".") in ("", wanted))


def dmarc_temporary(msg: EmailMessage, address: str, trusted: list[str]) -> bool:
    """The provider's own header (as authenticated reads it) holds one dmarc result, temperror, for address's domain.
    authenticated then falls back to dkim / spf alignment; a mail that fails it too is passed over and the owner is
    asked to send it again (P25, int2 Q1)."""
    servers = {x.strip().lower() for x in trusted if x.strip()}
    for value in msg.get_all("Authentication-Results") or []:
        try:
            server, results = parse_auth_results(str(value))
        except Exception:
            return False
        if server is None or server not in servers:
            continue
        if not _plain_header(str(value)):
            return False
        dmarc = [r for r in results if r.method == "dmarc"]
        return len(dmarc) == 1 and _temporary(dmarc[0], _domain(address))
    return False


def sender_of(msg: EmailMessage) -> str | None:
    """The one From address, lower case; None when there is none or more than one."""
    found = [a for _, a in getaddresses(msg.get_all("From") or []) if a]
    return found[0].strip().lower() if len(found) == 1 else None


def _is_web(url: str) -> bool:
    return parsable(url) and urlsplit(url).scheme.lower() in ("http", "https")


def _read_html(html: str) -> tuple[str, int, str | None, set[str]]:
    """One parse of an HTML part: its text, how many links it has, its "view in browser" link (the first http(s) link
    whose text says so) and the links whose text says unsubscribe (in either language)."""
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)
    view, unsubscribe = None, set()
    for a in anchors:
        href, label = a["href"].strip(), a.get_text(" ", strip=True)
        if not _is_web(href):
            continue
        if view is None and _VIEW_ONLINE.search(label):
            view = normalize_url(href, trim=False)
        if _UNSUBSCRIBE.search(label):
            unsubscribe.add(normalize_url(href))
    return soup.get_text("\n", strip=True), len(anchors), view, unsubscribe


def view_online_link(html: str) -> str | None:
    """A newsletter's "view in browser" link: the first http(s) link whose text says so."""
    return _read_html(html)[2]


def _content(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, ValueError):  # an unknown charset: what can be read of it
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def read_mail(msg: EmailMessage, depth: int = 0) -> Mail:
    """What a mail holds (slow on a big newsletter: callers run it in a thread). A mail attached to it
    (message/rfc822: "forward as attachment") is read as a forward: its words, its HTML, its files and its sender."""
    name, address = parseaddr(str(msg.get("From", "")))
    plain = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    html = _content(html_part) if html_part is not None else None
    html_text, anchors, view, skip = _read_html(html) if html else ("", 0, None, set())
    text = _content(plain) if plain is not None else html_text
    files, attached = [], []
    for part in msg.iter_attachments():
        if part.get_content_type() == "message/rfc822" and depth < MAX_FORWARD_DEPTH:
            inner = part.get_content()
            if isinstance(inner, EmailMessage):
                attached.append(inner)
                continue
        if part.get_content_maintype() in ("multipart", "message"):
            continue
        filename = part.get_filename()
        inline = part.get_content_disposition() != "attachment"
        if inline and (part.get("Content-ID") or not filename):  # a picture the HTML shows (a logo …)
            continue
        data = part.get_payload(decode=True) or b""
        if data:  # never an empty file
            files.append(MailFile(filename or "file", part.get_content_type(), data))
    for value in msg.get_all("List-Unsubscribe") or []:
        skip.update(normalize_url(u.strip()) for u in _BRACKETED.findall(str(value)) if _is_web(u.strip()))
    raw_subject = str(msg.get("Subject", "") or "")
    subject = _SUBJECT_PREFIX.sub("", raw_subject).strip()
    forwarded = bool(_FORWARD_MARK.search(text)) or bool(re.match(r"\s*(?:fwd?|fw|转发)\s*[:：]", raw_subject, re.I))  # zh: content
    original = None
    if forwarded and (m := _FROM_LINE.search(text)):
        oname, oaddr = parseaddr(m.group(1).strip())
        original = oname or oaddr or None
    mail = Mail(sender=address.strip().lower(), sender_name=name.strip(), subject=subject,
                message_id=(str(msg.get("Message-ID", "")).strip() or None), text=text, html=html, files=files,
                forwarded=forwarded, original_sender=original, html_only=plain is None and html is not None,
                anchors=anchors, view_url=view, skip_links=skip)
    if attached:
        inner = read_mail(attached[0], depth + 1)
        mail.text = "\n\n".join(x for x in (text.strip(), inner.text.strip()) if x)
        mail.html, mail.html_only, mail.anchors, mail.view_url = (inner.html, inner.html_only, inner.anchors,
                                                                  inner.view_url)
        mail.files += inner.files
        mail.skip_links |= inner.skip_links
        mail.forwarded = True
        mail.original_sender = inner.sender_name or inner.sender or None
        mail.subject = subject or inner.subject
    return mail


def _own_words(text: str) -> str:
    """What the mail says besides its links and a forward's header lines."""
    return _HEADER_LINE.sub("", _FORWARD_MARK.sub("", without_urls(text))).strip()


def _web_links(mail: Mail) -> list[str]:
    """The links to save: never an unsubscribe or tracking one (named in List-Unsubscribe, a link whose text or line
    says unsubscribe in either language, or whose path says so)."""
    out = []
    for line in mail.text.splitlines():
        if _UNSUBSCRIBE.search(without_urls(line)):
            continue
        for raw in extract_urls(line):
            url = normalize_url(raw)
            if (_is_web(url) and url not in out and url not in mail.skip_links
                    and not _UNSUB_PATH.search(urlsplit(url).path + "?" + urlsplit(url).query)):
                out.append(url)
    return out


def plan_mail(mail: Mail) -> Plan:
    """How a mail is saved (pure, but callers run it in a thread with the reading)."""
    links = _web_links(mail)
    words = _own_words(mail.text)
    newsletter = mail.html is not None and (mail.forwarded or mail.html_only or mail.anchors >= NEWSLETTER_LINKS)
    plan = Plan(None, title=mail.subject, files=list(mail.files), caption=mail.subject)
    if links and len(words) < LINK_ONLY_CHARS:
        plan.kind, plan.links = LINKS, links[:MAX_LINKS]
    elif newsletter and words:
        plan.kind, plan.url, plan.text = ARTICLE, mail.view_url, mail.text.strip()
    elif mail.files:  # the words go with the files, as a caption does in chat
        plan.kind, plan.caption = FILES, "\n".join(x for x in (mail.subject, mail.text.strip()) if x)
    elif words:
        plan.kind, plan.text, plan.links_unread = NOTE, mail.text.strip(), bool(links)
    return plan


def user_of(row_id: int) -> str:
    return f"mail-{row_id}"


def row_of(user_id: str) -> int | None:
    m = re.fullmatch(r"mail-(\d{1,18})", user_id)
    return int(m.group(1)) if m else None


class MailSaves:
    """One row per mail looked at: its place in the mailbox (the UID high-water mark per UIDVALIDITY), who to answer,
    the jobs saving it and the replies waiting for them. A mail not taken (not the owner's) keeps no address."""

    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    def last_uid(self, uidvalidity: int) -> int | None:
        """The high-water mark: the newest mail done with. A mail whose saves were cut off (still 'open': a stop in
        the middle) keeps the mark below it, so it is taken again."""
        (open_uid,) = self.conn.execute("SELECT MIN(uid) FROM email_saves WHERE uidvalidity = ? AND state = 'open'",
                                        (uidvalidity,)).fetchone()
        (last,) = self.conn.execute("SELECT MAX(uid) FROM email_saves WHERE uidvalidity = ?"
                                    + (" AND uid < ?" if open_uid is not None else ""),
                                    (uidvalidity,) + ((open_uid,) if open_uid is not None else ())).fetchone()
        return last

    def find(self, uidvalidity: int, uid: int) -> dict | None:
        row = self.conn.execute("SELECT id FROM email_saves WHERE uidvalidity = ? AND uid = ?",
                                (uidvalidity, uid)).fetchone()
        return self.get(row[0]) if row else None

    def record(self, rid: int, key: str, job: int | None = None) -> None:
        """A part of the mail's saves is done (key), with the job saving it. Call it in the same step (no await) as
        the enqueue, or inside its transaction."""
        row = self.get(rid)
        self.update(rid, done=row["done"] + [key], jobs=row["jobs"] + ([job] if job is not None else []))

    def add(self, uidvalidity: int, uid: int, mail: Mail | None) -> int | None:
        """The new row's id; None when this mail was looked at before."""
        try:
            rid = self.conn.execute(
                "INSERT INTO email_saves(uidvalidity, uid, sender, subject, message_id, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uidvalidity, uid, mail.sender if mail else None, mail.subject if mail else "",
                 mail.message_id if mail else None, "open" if mail else "ignored", self.clock())).lastrowid
        except sqlite3.IntegrityError:
            return None
        self.conn.execute("DELETE FROM email_saves WHERE id <= ? AND state IN ('replied', 'ignored')",
                          (rid - KEEP_ROWS,))
        for table in ("focus", "pending_manual"):  # what App keeps per user (mail-<id>): nobody reads it later
            self.conn.execute(f"DELETE FROM {table} WHERE channel = ? AND user_id NOT IN "
                              "(SELECT 'mail-' || id FROM email_saves WHERE state != 'replied')", (EMAIL,))
        return rid

    def get(self, rid: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM email_saves WHERE id = ?", (rid,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["jobs"], out["replies"], out["done"] = (json.loads(out["jobs"]), json.loads(out["replies"]),
                                                    json.loads(out["done"]))
        return out

    def update(self, rid: int, **values) -> None:
        for key in ("jobs", "replies", "done"):
            if key in values:
                values[key] = json.dumps(values[key], ensure_ascii=False)
        sets = ", ".join(f"{k} = ?" for k in values)
        self.conn.execute(f"UPDATE email_saves SET {sets} WHERE id = ?", (*values.values(), rid))

    def jobs_done(self, jobs: list[int]) -> bool:
        if not jobs:
            return True
        row = self.conn.execute(f"SELECT COUNT(*) FROM jobs WHERE id IN ({','.join('?' * len(jobs))}) "
                                "AND status IN ('queued', 'running')", jobs).fetchone()
        return row[0] == 0


SendMail = Callable[[str, str, str | None, str], Awaitable[None]]  # (to, subject, in_reply_to, body); raises


class MailIntake:
    def __init__(self, app: App, conf: EmailConfig, send_mail: SendMail | None = None):
        self.app = app
        self.conf = conf
        self.send_mail = send_mail
        self.saves = MailSaves(app.conn)
        self._told_no_telegram = False
        self._sending = asyncio.Lock()

    def last_uid(self, uidvalidity: int) -> int | None:
        return self.saves.last_uid(uidvalidity)

    async def skip(self, uidvalidity: int, uid: int, why: str = "") -> None:
        """A mail not taken: only its place is kept (the high-water mark moves past it). One whose save was cut off
        (an open row: owner_addresses changed since, or it can no longer be fetched) is closed."""
        if self.saves.add(uidvalidity, uid, None) is None:
            row = self.saves.find(uidvalidity, uid)
            if row is not None and row["state"] == "open":
                await self.close(row, why)

    def open_rows(self, uidvalidity: int) -> list[dict]:
        """Mails whose save a stop cut off: they hold the mark until finished or closed."""
        return [self.saves.get(r[0]) for r in self.saves.conn.execute(
            "SELECT id FROM email_saves WHERE uidvalidity = ? AND state = 'open' ORDER BY uid", (uidvalidity,))]

    async def close(self, row: dict, why: str) -> None:
        """Gives up finishing a cut-off save (its mail is gone, rejected now, or cannot be fetched): what was queued
        goes on and is answered; the mark moves past it. One log line."""
        log.info("email: an unfinished save #%d dropped (%s); what was already queued goes on", row["id"], why)
        self.saves.update(row["id"], state="waiting")
        await self._answer_now(row["id"])

    # ---- a mail in ----

    async def save(self, uidvalidity: int, uid: int, mail: Mail) -> int | None:
        """Queues the mail's saves; None when it was taken before (a restart between saving and marking it read). A
        mail whose saves were cut off by a stop (its row still 'open') is finished: what was queued is not queued
        again. The row leaves 'open' (and the mark moves past it) only once every save is queued."""
        row = self.saves.find(uidvalidity, uid)
        if row is not None and row["state"] != "open":
            return None
        rid = row["id"] if row is not None else self.saves.add(uidvalidity, uid, mail)
        if rid is None:
            return None
        done = set(row["done"]) if row is not None else set()
        user, plan, outcome = user_of(rid), Plan(None), "error"
        try:
            plan = await asyncio.to_thread(plan_mail, mail)  # parses no HTML now, but never on the loop
            outcome = await self._save(rid, user, mail, plan, done)
        except Exception:  # not a stop (CancelledError): that leaves the row open, to be finished after it
            log.exception("email save #%d failed", rid)
            await self.app.send(EMAIL, user, t("mail.save_error"))
        self._log_in(rid, mail, plan, outcome)
        self.saves.update(rid, state="waiting")
        self.app.wake.set()
        await self._answer_now(rid)
        return rid

    async def _answer_now(self, rid: int) -> None:
        """The reply when nothing is left to wait for (a duplicate link …); when it cannot go, the outbox sweep sends
        it again (its text is kept, so the whole reply)."""
        if not (await self._flush(rid)).ok:
            row = self.saves.get(rid)
            self.app.outbox.add(EMAIL, user_of(rid), row["replies"][-1])

    def _enqueue(self, rid: int, key: str, kind: str, payload: dict, user: str) -> None:
        """A job and the row's record of it, in one transaction: queued and recorded, or neither."""
        with transaction(self.app.conn):
            self.saves.record(rid, key, self.app.jobs.enqueue(kind, payload, EMAIL, user))

    async def _save(self, rid: int, user: str, mail: Mail, plan: Plan, done: set[str]) -> str:
        """Each part of the plan once (done: the parts queued before a stop cut the save off)."""
        app, said = self.app, []
        if plan.kind == LINKS:
            for url in plan.links:
                key = f"link:{url}"
                if key in done:
                    continue
                what, job, entry = await app.queue_url(url, EMAIL, user)
                # no await since queue_url enqueued: the job and its record go together
                self.saves.record(rid, key, job if what in ("queued", "refetch") else None)
                said.append(what)
                if what == "duplicate":
                    await app.send(EMAIL, user, format_duplicate(entry.id, entry.title))
                elif what == "in progress":
                    await app.send(EMAIL, user, t("app.in_progress"))
        elif plan.kind == ARTICLE and "article" not in done:
            if (entry := await self._saved(plan.url)) is not None:
                await app.send(EMAIL, user, format_duplicate(entry.id, entry.title))
                self.saves.record(rid, "article")
                said.append("duplicate")
            else:
                page = await self._stage([MailFile("page.html", "text/html", (mail.html or "").encode("utf-8"))])
                self._enqueue(rid, "article", "page", {
                    "page": str(page[0].path), "url": plan.url, "title": plan.title or None, "text": plan.text,
                    "author": mail.original_sender or mail.sender_name or None}, user)
                said.append("article")
        elif plan.kind == NOTE and "note" not in done:  # never meant as a question: no "Make it a question" (from_chat)
            self._enqueue(rid, "note", "note", {"text": plan.text, "title": plan.title or None, "from_chat": True,
                                                **({"links_unread": True} if plan.links_unread else {})}, user)
            said.append("note")
        if plan.files and "files" not in done:
            files = [attachment_to_dict(a) for a in await self._stage(plan.files)]
            self._enqueue(rid, "files", "attachments", {"files": files, "caption": plan.caption,
                                                        "entry_id": app.pipeline.reserve_entry_id()}, user)
            said.append("files")
        if not said and not done:
            await app.send(EMAIL, user, t("mail.nothing_to_save"))
            return "nothing"
        return ",".join(said) or "resumed"

    async def _saved(self, url: str | None):
        """The entry already saved from this "view in browser" link (one that still waits for its content is not)."""
        existing = self.app.index.find_by_url(url) if url else None
        path = await self.app.pipeline.locate(existing) if existing else None
        entry = self.app.library.load(path) if path else None
        return entry if entry is not None and entry.status != "needs_manual" else None

    async def _stage(self, files: list[MailFile]) -> list[Attachment]:
        """Each file in its own folder under the incoming dir (as the chat channels stage theirs); off the loop."""
        root = self.app.cfg.incoming_dir

        def write() -> list[Attachment]:
            out = []
            for f in files:
                folder = root / f"mail-{uuid.uuid4().hex}"
                folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    path = folder / safe_filename(f.filename, "file")
                    path.write_bytes(f.data)
                except BaseException:
                    shutil.rmtree(folder, ignore_errors=True)
                    raise
                major = f.mime.split("/")[0]
                out.append(Attachment(major if major in ("image", "audio", "video") else "file", path, path.name,
                                      f.mime, size=len(f.data)))
            return out

        return await asyncio.to_thread(write)

    def _log_in(self, rid: int, mail: Mail, plan: Plan, outcome: str) -> None:
        """`in email <tag> #<id> <kind> <n><chars> [+k<files>] -> <outcome> "<subject, ≤ 20 chars>"` (the unit
        words are fixed in Chinese): the sender only as a hash tag. Never raises."""
        try:
            files = f" +{len(plan.files)}文件" if plan.files else ""  # zh: log
            log.info('in %s %s #%d %s %d字%s -> %s "%s"', EMAIL, user_tag(mail.sender), rid, plan.kind or "nothing",
                     len(mail.text), files, outcome, log_preview(mail.subject))  # zh: log
        except Exception:
            log.debug("logging a mail failed", exc_info=True)

    # ---- the jobs' replies ----

    async def reply(self, user_id: str, text: str) -> SendResult:
        """A reply App sends about a mail: kept until every save of that mail is done, then one reply for all of
        them. A text already kept (the outbox sending it again) is not kept twice."""
        rid = row_of(user_id)
        row = self.saves.get(rid) if rid is not None else None
        if row is None:
            return SendResult(True)  # its row is gone: nobody to answer
        if text not in row["replies"]:
            self.saves.update(rid, replies=row["replies"] + [text])
        return await self._flush(rid)  # not ok: App keeps this text in its outbox, and sending it again sends all

    async def _flush(self, rid: int) -> SendResult:
        """Sends what is kept once every job of the mail is done; after the reply, a late text (a job run again, one
        that came while the reply was being sent) goes on its own."""
        async with self._sending:
            row = self.saves.get(rid)
            if row is None or not row["replies"] and row["state"] == "replied":
                return SendResult(True)
            if row["state"] not in ("waiting", "replied") or not self.saves.jobs_done(row["jobs"]):
                return SendResult(True)
            result = await self._deliver(row, row["replies"])
            if result.ok:
                sent = row["replies"]
                self.saves.update(rid, state="replied",
                                  replies=[x for x in self.saves.get(rid)["replies"] if x not in sent])
            return result

    async def _deliver(self, row: dict, texts: list[str]) -> SendResult:
        if not texts or self.conf.reply == "none":
            return SendResult(True)
        body = "\n\n".join(texts)
        if m := re.search(r"^id(?:：|: )(\d{8}-[0-9a-f]{6})$", body, re.M):  # either language  # zh: input
            hint = t("extension.add_hint", id=m.group(1))
            body = body.replace(t("formatting.merge_ask"), hint).replace(t("formatting.supplement_ask"), hint)
        if self.conf.reply == "telegram":
            return await self._to_telegram(body)
        subject = REPLY_SUBJECT.format(subject=row["subject"]) if row["subject"] else t("mail.reply_subject_none")
        status = "ok"
        try:
            await self.send_mail(row["sender"], subject, row["message_id"], body)
        except Exception as e:
            status = "failed(error)"
            log.warning("email: the reply to #%d could not be sent: %s", row["id"], type(e).__name__)
        try:
            log.info(out_line(EMAIL, row["sender"], body, status))
        except Exception:
            log.debug("logging a reply failed", exc_info=True)
        return SendResult(status == "ok", reason="" if status == "ok" else "error")

    async def tell_skipped(self, subject: str) -> None:
        """A mail the server would not hand over was passed over: the owner is told once, on Telegram (when replies
        go by email too: the box may be what fails), never when replies are off."""
        if self.conf.reply == "none":
            return
        shown = subject if len(subject) <= SKIPPED_SUBJECT_CHARS else subject[:SKIPPED_SUBJECT_CHARS] + "…"
        await self._to_telegram(t("mail.skipped", subject=shown or t("mail.no_subject")))

    async def tell_later(self, subject: str) -> None:
        """A mail the provider could not check just now (dmarc=temperror): the owner is asked once to send it again,
        on Telegram like tell_skipped (never by mail: the sender is not proven), never when replies are off."""
        if self.conf.reply == "none":
            return
        shown = subject if len(subject) <= SKIPPED_SUBJECT_CHARS else subject[:SKIPPED_SUBJECT_CHARS] + "…"
        await self._to_telegram(t("mail.dmarc_later", subject=shown or t("mail.no_subject")))

    async def _to_telegram(self, body: str) -> SendResult:
        conf = self.app.cfg.channels.get("telegram")
        if "telegram" not in self.app.channels or conf is None or not conf.enabled or not conf.allowed_users:
            if not self._told_no_telegram:  # once: every mail would say it again
                log.info("email: reply = \"telegram\" but Telegram is not running here; not sent")
                self._told_no_telegram = True
            return SendResult(True)
        for owner in conf.allowed_users:  # only the owner's own accounts
            await self.app.send("telegram", owner, body)  # held in the outbox when it cannot go now
        return SendResult(True)
