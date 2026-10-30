"""The email channel (P23): save-only. Polls one dedicated mailbox over IMAP (stdlib imaplib, in a thread) and sends
the replies over SMTP. What a mail is saved as and who may send one: octopus.mail.

Each poll: log in, select INBOX, read its UIDVALIDITY; the mails after the UID high-water mark kept for that
UIDVALIDITY (or, the first time, the unread ones) are taken in UID order, each marked \\Seen once its saves are queued.
A mail not from an owner, not authenticated by the provider, an automatic one or too big is passed over (its place
kept, left unread) with one log line per sender. Nothing of an address goes to the log but a short hash tag."""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
import smtplib
import ssl
import threading
import time
from email import policy
from email.message import EmailMessage
from typing import Callable

from octopus.channels.base import OnMessage, Reply, SendResult, user_tag
from octopus.config import EmailConfig
from octopus.mail import authenticated, dmarc_temporary, read_mail, sender_of, trusted_servers

log = logging.getLogger(__name__)
NETWORK_TIMEOUT_SECONDS = 30  # each socket operation, IMAP and SMTP
SMTP_TOTAL_SECONDS = 90  # one whole reply (connect, TLS, login, send): the reply lock is never held longer
MAX_MAIL_BYTES = 40 * 1024 * 1024
MAX_PER_POLL = 50
FETCH_ATTEMPTS = 3  # a mail whose FETCH is refused this many times …
REFUSAL_WAIT_SECONDS = 600  # … and still refused this long after the first time is passed over (a short outage is not)
MAILBOX = "INBOX"
_UIDVALIDITY = re.compile(rb"UIDVALIDITY (\d+)")
_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
# the provider could not look the sender's DMARC record up and dkim / spf do not align either: passed over, and the
# owner is asked to send it again (P25)
LATER = "the provider could not check the sender (dmarc=temperror) and dkim/spf do not align"
HEAD = "(RFC822.SIZE BODY.PEEK[HEADER])"
BODY = "(BODY.PEEK[])"


class _Refused(imaplib.IMAP4.error):
    """The server answered a FETCH with something other than a clean OK."""


def _tls() -> ssl.SSLContext:
    """The server's certificate and host name are checked (the stdlib's default for imaplib / smtplib is not)."""
    return ssl.create_default_context()


def _open_imap(host: str, port: int):
    if port == 143:
        imap = imaplib.IMAP4(host, port, timeout=NETWORK_TIMEOUT_SECONDS)
        imap.starttls(ssl_context=_tls())
        return imap
    return imaplib.IMAP4_SSL(host, port, ssl_context=_tls(), timeout=NETWORK_TIMEOUT_SECONDS)


def _open_smtp(host: str, port: int):
    if port == 465:
        return smtplib.SMTP_SSL(host, port, context=_tls(), timeout=NETWORK_TIMEOUT_SECONDS)
    smtp = smtplib.SMTP(host, port, timeout=NETWORK_TIMEOUT_SECONDS)
    smtp.starttls(context=_tls())
    return smtp


def _one_line(text: str) -> str:
    return " ".join(_CONTROL.sub(" ", text).split())


class EmailChannel:
    name = "email"
    acks_saves = False

    def __init__(self, conf: EmailConfig, password: str, imap_factory: Callable = _open_imap,
                 smtp_factory: Callable = _open_smtp):
        self.conf = conf
        self.password = password
        self.imap_factory = imap_factory
        self.smtp_factory = smtp_factory
        self.trusted = trusted_servers(conf)  # whose Authentication-Results header is the provider's
        self.intake = None  # mail.MailIntake, set by `octopus run` once the App exists
        self._passed_over: set[str] = set()  # senders already logged
        self._failing = False  # the last poll could not reach the mailbox (logged once until it can)
        self._refusals: dict[tuple[int, int], tuple[int, float]] = {}  # (uidvalidity, uid) -> (refused, since)
        self.clock: Callable[[], float] = time.monotonic  # the refusal floor's clock, injectable for tests

    async def start(self, on_message: OnMessage) -> None:
        """Polls until cancelled. Mails never go through on_message: they are not chat."""
        while True:
            if self.intake is not None:
                await self.poll_once()
            await asyncio.sleep(self.conf.poll_seconds)

    async def send(self, user_id: str, reply: Reply) -> SendResult:
        """A save job's reply: one per mail once all its saves are done (MailIntake)."""
        if self.intake is None:
            return SendResult(False, reason="no-context")
        return await self.intake.reply(user_id, reply.text)

    # ---- IMAP ----

    def _scrub(self, text: str) -> str:
        for secret in (self.password, self.conf.username):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _connect(self):
        imap = self.imap_factory(self.conf.imap_host, self.conf.imap_port)
        try:
            imap.login(self.conf.username, self.password)
            typ, _ = imap.select(MAILBOX)
            if typ != "OK":
                raise imaplib.IMAP4.error(f"cannot select {MAILBOX}")
            typ, data = imap.status(MAILBOX, "(UIDVALIDITY)")
            m = _UIDVALIDITY.search(b" ".join(d for d in data if isinstance(d, bytes))) if typ == "OK" else None
            if m is None:
                raise imaplib.IMAP4.error("no UIDVALIDITY")
            return imap, int(m.group(1))
        except BaseException:
            self._logout(imap)
            raise

    @staticmethod
    def _logout(imap) -> None:
        try:
            imap.logout()
        except Exception:
            log.debug("email: logout failed", exc_info=True)

    @staticmethod
    def _search(imap, last: int | None) -> list[int]:
        """The unread mails the first time (no mark for this UIDVALIDITY yet), else every mail after the mark."""
        typ, data = imap.uid("SEARCH", None, "UNSEEN" if last is None else f"UID {last + 1}:*")
        if typ != "OK":
            raise imaplib.IMAP4.error("search failed")
        uids = sorted({int(u) for u in b" ".join(d for d in data if d).split() if u.isdigit()})
        return [u for u in uids if last is None or u > last][:MAX_PER_POLL]  # "N:*" also names the last mail

    @staticmethod
    def _fetch(imap, uid: int, what: str) -> tuple[bytes, bytes]:
        """(every non-literal part of the answer, the literal) of HEAD or BODY, never marking the mail read: the
        RFC822.SIZE may come before or after the literal. Raises _Refused on anything but a clean answer (Gmail's
        NO [UNAVAILABLE] …): the poll stops and the mail is tried again next time (FETCH_ATTEMPTS polls at most)."""
        typ, data = imap.uid("FETCH", str(uid), what)
        literal, rest = None, []
        for item in (data or []) if typ == "OK" else []:
            if isinstance(item, tuple) and len(item) == 2 and literal is None:
                rest.append(item[0])
                literal = item[1]
            elif isinstance(item, bytes):
                rest.append(item)
        if literal is None:
            raise _Refused(f"fetch {what} answered {typ}")
        return b" ".join(rest), literal

    @staticmethod
    def _exists(imap, uid: int) -> bool:
        typ, data = imap.uid("SEARCH", None, f"UID {uid}")
        if typ != "OK":
            raise imaplib.IMAP4.error("search failed")
        return str(uid).encode() in b" ".join(d for d in data if d).split()

    @staticmethod
    def _mark_seen(imap, uid: int) -> None:
        try:
            imap.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
        except Exception:
            log.info("email: could not mark a mail read", exc_info=True)

    async def poll_once(self) -> int:
        """One look at the mailbox; returns how many mails were taken. Never raises but for cancellation."""
        taken = 0
        try:
            imap, uidvalidity = await asyncio.to_thread(self._connect)
        except Exception as e:
            if not self._failing:
                log.warning("email: cannot read the mailbox: %s: %s", type(e).__name__, self._scrub(str(e))[:200])
            self._failing = True
            return 0
        if self._failing:
            log.info("email: the mailbox can be read again")
            self._failing = False
        try:
            for row in self.intake.open_rows(uidvalidity):  # a save cut off by a stop: its mail may be gone since
                if not await asyncio.to_thread(self._exists, imap, row["uid"]):
                    await self.intake.close(row, "its mail is no longer in INBOX")
            uids = await asyncio.to_thread(self._search, imap, self.intake.last_uid(uidvalidity))
            for uid in uids:
                taken += await self._take(imap, uidvalidity, uid)
        except Exception as e:  # the connection dropped, a FETCH failed: the next poll goes on from the mark
            log.warning("email: reading the mailbox stopped: %s: %s", type(e).__name__, self._scrub(str(e))[:200])
        finally:
            await asyncio.to_thread(self._logout, imap)
        return taken

    async def _pass_over(self, uidvalidity: int, uid: int, sender: str | None, why: str) -> None:
        """Not taken: its place is kept (never looked at again) and it stays unread; one log line per sender, and one
        for each unreadable mail (each is passed over once)."""
        await self.intake.skip(uidvalidity, uid, why)
        key = sender or "-"
        if key not in self._passed_over or sender is None:
            self._passed_over.add(key)
            log.info("in email %s ignored: %s", user_tag(sender) if sender else "-", why)

    def _judge(self, head: bytes) -> tuple[str | None, str | None, str]:
        """(the sender, why the mail is not taken or None, its subject) from its headers alone; in a thread. A header
        no parser can take apart is "unreadable", never an error that stops the poll."""
        try:
            msg = email.message_from_bytes(head, policy=policy.default)
            sender = sender_of(msg)
            subject = _one_line(str(msg.get("Subject", "") or ""))
            owners = {a.strip().lower() for a in self.conf.owner_addresses}
            if sender is None or sender not in owners:
                return sender, "not an owner address", subject
            if sender == self.conf.username.strip().lower() or str(msg.get("Auto-Submitted", "no")).lower() != "no":
                return sender, "an automatic mail or our own", subject  # never answer a reply
            if not authenticated(msg, sender, self.trusted):
                if dmarc_temporary(msg, sender, self.trusted):
                    return sender, LATER, subject
                return sender, "the provider did not authenticate the sender (dkim/spf)", subject
            return sender, None, subject
        except Exception:
            log.info("email: could not read a mail's headers", exc_info=True)
            return None, "unreadable", ""

    @staticmethod
    def _read(raw: bytes):
        return read_mail(email.message_from_bytes(raw, policy=policy.default))

    async def _take(self, imap, uidvalidity: int, uid: int) -> int:
        """1 when the mail was taken (its saves queued, then marked read), else 0. A mail done with before (found again
        while a cut-off save held the mark) is not downloaded again."""
        row = self.intake.saves.find(uidvalidity, uid)
        if row is not None and row["state"] != "open":
            if row["state"] != "ignored":  # taken: make sure it shows as read; passed over: left unread
                await asyncio.to_thread(self._mark_seen, imap, uid)
            return 0
        subject = ""
        try:
            line, head = await asyncio.to_thread(self._fetch, imap, uid, HEAD)
            sender, why, subject = await asyncio.to_thread(self._judge, head)
            if why == LATER:  # recorded first, so a restart never says it again; told once per mail
                await self.intake.skip(uidvalidity, uid, why)
                log.info("in email %s not taken: %s", user_tag(sender), why)
                await self.intake.tell_later(subject)
                return 0
            if why is not None:
                await self._pass_over(uidvalidity, uid, sender, why)
                return 0
            m = _SIZE.search(line)
            if m is None or int(m.group(1)) > MAX_MAIL_BYTES:
                await self._pass_over(uidvalidity, uid, sender, "too big")
                return 0
            _, raw = await asyncio.to_thread(self._fetch, imap, uid, BODY)
        except _Refused:
            key = (uidvalidity, uid)
            refused, since = self._refusals.get(key, (0, self.clock()))
            self._refusals[key] = (refused + 1, since)
            if refused + 1 < FETCH_ATTEMPTS or self.clock() - since < REFUSAL_WAIT_SECONDS:
                raise  # the poll stops; tried again next time
            del self._refusals[key]
            await self._pass_over(uidvalidity, uid, None, f"could not be fetched {refused + 1} times")
            await self.intake.tell_skipped(subject)  # the owner hears of it once
            return 0
        self._refusals.pop((uidvalidity, uid), None)
        try:
            mail = await asyncio.to_thread(self._read, raw)
        except Exception:  # a mail no reader can take apart: never tried again on every poll
            log.exception("email: could not read a mail")
            await self._pass_over(uidvalidity, uid, None, "unreadable")
            return 0
        rid = await self.intake.save(uidvalidity, uid, mail)
        await asyncio.to_thread(self._mark_seen, imap, uid)  # also one taken before a restart came between
        return 1 if rid is not None else 0

    # ---- SMTP ----

    async def send_mail(self, to: str, subject: str, in_reply_to: str | None, body: str) -> None:
        """One reply mail; raises when it could not be sent."""
        msg = EmailMessage()
        msg["From"] = self.conf.username
        msg["To"] = to
        msg["Subject"] = _one_line(subject)  # a decoded subject may hold CR/LF
        msg["Auto-Submitted"] = "auto-replied"  # RFC 3834: mail software must not answer it
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)

        lock, state = threading.Lock(), {"given_up": False, "handing_over": False}

        def deliver() -> None:
            smtp = self.smtp_factory(self.conf.smtp_host, self.conf.smtp_port)
            try:
                smtp.login(self.conf.username, self.password)
                with lock:  # past the time limit it never starts handing the mail over: the retry is the only send
                    if state["given_up"]:
                        return
                    state["handing_over"] = True
                smtp.send_message(msg)
            finally:
                try:
                    smtp.quit()
                except Exception:
                    log.debug("email: smtp quit failed", exc_info=True)

        try:
            await asyncio.wait_for(asyncio.to_thread(deliver), SMTP_TOTAL_SECONDS)
        except TimeoutError:
            # the thread cannot be stopped; it ends on its own (its sockets time out)
            with lock:
                state["given_up"] = True
                handing_over = state["handing_over"]
            if not handing_over:
                raise  # nothing was handed over: tried again from the outbox
            # it may still arrive: counted as sent, never sent twice
            log.warning("email: a reply timed out while being handed to the SMTP server; it may have been sent, "
                        "not tried again")
