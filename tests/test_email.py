"""P23: the save-only email channel end to end, with a fake IMAP mailbox and a fake SMTP server (never a connection)."""
import dataclasses
import imaplib
import logging

import pytest
from fakes import FakeChannel, FakeEmbedder, FakeProvider, StubFetcher, fake_media, summary_json
from test_mail import LONG_TEXT, NEWSLETTER_HTML, OWNER, make
from test_pipeline_page import WebStub

from octopus.app import App
from octopus.bootstrap import build_channels
from octopus.channels.base import user_tag
from octopus.channels.email import MAX_MAIL_BYTES, EmailChannel
from octopus.config import ChannelConfig, ConfigError, EmailConfig, load_config, make_test_config
from octopus.db import connect
from octopus.fetchers import FetcherRegistry
from octopus.fetchers.base import FetchResult
from octopus.index import Index
from octopus.library import Library
from octopus.llm.router import LLMRouter
from octopus.mail import NOTHING_TO_SAVE, MailIntake
from octopus.settings import Settings

BOX = "octopus@example.com"
PASSWORD = "app-password-123"


class FakeIMAP:
    """One INBOX: uid -> (raw bytes, flags). Records what was asked."""

    def __init__(self, uidvalidity=7, fail_login=False):
        self.uidvalidity = uidvalidity
        self.mails: dict[int, list] = {}
        self.fail_login = fail_login
        self.fail_fetch: set[int] = set()  # these answer NO to a body FETCH (Gmail's [UNAVAILABLE])
        self.size_last = False  # put RFC822.SIZE after the header literal (RFC 3501 fixes no order)
        self.calls = []
        self.logged_out = 0

    def add(self, msg, seen=False) -> int:
        uid = max(self.mails, default=0) + 1
        self.mails[uid] = [msg.as_bytes() if hasattr(msg, "as_bytes") else msg, {"\\Seen"} if seen else set()]
        return uid

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self.fail_login:
            raise imaplib.IMAP4.error(f"[AUTHENTICATIONFAILED] Invalid credentials for {user}")

    def select(self, box):
        return "OK", [str(len(self.mails)).encode()]

    def status(self, box, what):
        return "OK", [f"INBOX (UIDVALIDITY {self.uidvalidity})".encode()]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            crit = args[1]
            if crit == "UNSEEN":
                found = [u for u, (_, f) in self.mails.items() if "\\Seen" not in f]
            elif ":" not in crit:  # "UID n": that one mail, if it is still there
                found = [u for u in self.mails if u == int(crit.split()[1])]
            else:
                start = int(crit.split()[1].split(":")[0])
                found = [u for u in self.mails if u >= start] or ([max(self.mails)] if self.mails else [])
            return "OK", [" ".join(map(str, found)).encode()]
        if command == "FETCH":
            uid, what = int(args[0]), args[1]
            raw = self.mails[uid][0]
            if what == "(RFC822.SIZE BODY.PEEK[HEADER])":
                head = raw.split(b"\n\n", 1)[0] + b"\n\n" if b"\r\n\r\n" not in raw else raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                if self.size_last:
                    return "OK", [(f"1 (UID {uid} BODY[HEADER] {{{len(head)}}}".encode(), head),
                                  f" RFC822.SIZE {len(raw)})".encode()]
                return "OK", [(f"1 (UID {uid} RFC822.SIZE {len(raw)} BODY[HEADER] {{{len(head)}}}".encode(), head),
                              b")"]
            assert what == "(BODY.PEEK[])", what
            if uid in self.fail_fetch:
                return "NO", [b"[UNAVAILABLE] Temporary System Error"]
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        if command == "STORE":
            self.mails[int(args[0])][1].add("\\Seen")
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        self.logged_out += 1


class FakeSMTP:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def login(self, user, password):
        assert (user, password) == (BOX, PASSWORD)

    def send_message(self, msg):
        if self.fail:
            raise OSError("smtp down")
        self.sent.append(msg)

    def quit(self):
        pass


def build(tmp_path, reply="email", fetchers=None, telegram=False):
    cfg = make_test_config(tmp_path)
    cfg.email = EmailConfig(enabled=True, imap_host="imap.example.com", smtp_host="smtp.example.com", username=BOX,
                            owner_addresses=[OWNER.upper()], reply=reply, authserv_id=["mx.google.com"])
    imap, smtp = FakeIMAP(), FakeSMTP()
    ch = EmailChannel(cfg.email, PASSWORD, imap_factory=lambda h, p: imap, smtp_factory=lambda h, p: smtp)
    channels = {"email": ch}
    if telegram:
        cfg.channels["telegram"] = ChannelConfig(enabled=True, allowed_users=["owner1"])
        channels["telegram"] = FakeChannel("telegram")
    conn = connect(cfg.db_path, embed_dim=FakeEmbedder.dim)
    providers = {n: FakeProvider(n, summary_json(title="摘要标题")) for n in ("claude:haiku", "claude:sonnet", "local")}
    llm = LLMRouter(providers, {"summarize": cfg.summarize, "answer": cfg.answer}, Settings(conn))
    media = dataclasses.replace(fake_media(tmp_path), attachments_dir=cfg.attachments_dir)
    web = WebStub(FetchResult(url="https://a.com/x", source="web", title="网页标题", text="正文" * 200))
    app = App(cfg, conn=conn, library=Library(cfg.library_dir, cfg.trash_dir, cfg.attachments_dir),
              index=Index(conn, FakeEmbedder()), llm=llm,
              registry=FetcherRegistry(fetchers if fetchers is not None else [web]), channels=channels, media=media)
    ch.intake = MailIntake(app, cfg.email, ch.send_mail)
    app.imap, app.smtp, app.ch, app.web = imap, smtp, ch, web
    return app


async def run(app):
    taken = await app.ch.poll_once()
    await app.process_due_jobs()
    return taken


def entries(app):
    return [app.library.get(r[0]) for r in app.conn.execute("SELECT id FROM entries ORDER BY saved_at")]


# ---- what is taken ----

async def test_a_link_mail_is_saved_and_answered_once(tmp_path, caplog):
    app = build(tmp_path)
    caplog.set_level(logging.INFO)
    uid = app.imap.add(make("https://a.com/x", subject="Fwd: 看这篇"))
    assert await run(app) == 1
    [entry] = entries(app)
    assert entry.title == "网页标题" and entry.url == "https://a.com/x" and entry.channel == "email"
    [reply] = app.smtp.sent
    assert reply["To"] == OWNER and reply["Subject"] == "Re: 看这篇" and reply["In-Reply-To"] == "<abc@example.com>"
    assert reply["Auto-Submitted"] == "auto-replied" and reply["From"] == BOX
    assert "✅ 已保存：网页标题" in reply.get_content() and f"id：{entry.id}" in reply.get_content()
    assert "\\Seen" in app.imap.mails[uid][1]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert f"in email {user_tag(OWNER)} #1 links" in logged and '"看这篇"' in logged
    assert OWNER not in logged and BOX not in logged and PASSWORD not in logged


async def test_two_links_get_one_reply_when_both_are_done(tmp_path):
    app = build(tmp_path)
    app.imap.add(make("https://a.com/x\nhttps://b.com/y"))
    await app.ch.poll_once()
    await app.process_due_jobs()
    [reply] = app.smtp.sent
    assert reply.get_content().count("✅ 已保存") == 2


async def test_a_link_already_saved_is_answered_at_once(tmp_path):
    app = build(tmp_path)
    app.imap.add(make("https://a.com/x"))
    await run(app)
    app.imap.add(make("https://a.com/x"))
    await app.ch.poll_once()  # no job: the reply goes before any job runs
    assert len(app.smtp.sent) == 2 and "这个链接已经存过了" in app.smtp.sent[1].get_content()


async def test_a_newsletter_is_saved_as_an_article_from_its_html(tmp_path):
    app = build(tmp_path)
    text = ("---------- Forwarded message ---------\nFrom: Daily News <news@news.example.com>\n\n" + LONG_TEXT)
    app.imap.add(make(text, NEWSLETTER_HTML, subject="Fwd: 本周通讯"))
    await run(app)
    [entry] = entries(app)
    assert entry.source == "email" and entry.url == "https://news.example.com/p/1" and entry.title == "本周通讯"
    assert entry.author == "Daily News" and LONG_TEXT[:20] in entry.body
    assert app.web.calls == []  # read from the mail, never fetched
    assert list(app.cfg.incoming_dir.iterdir()) == []  # the staged page is gone once saved


async def test_an_html_mail_that_reads_as_nothing_keeps_its_words(tmp_path):
    app = build(tmp_path)
    html = "<html><body>" + "".join(f"<a href='https://x.example/{i}'>{i}</a>" for i in range(5)) + "</body></html>"
    app.imap.add(make(LONG_TEXT, html, subject="Fwd: 通讯"))
    await run(app)
    [entry] = entries(app)
    assert entry.source == "email" and LONG_TEXT[:20] in entry.body and entry.url is None


async def test_a_plain_mail_is_a_note_never_a_question(tmp_path):
    app = build(tmp_path)
    app.imap.add(make("今天想到：" + LONG_TEXT, subject="想法"))
    await run(app)
    [entry] = entries(app)
    assert entry.source == "manual" and entry.status == "ok"
    assert app.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0  # no 改成提问
    assert "改成提问" not in app.smtp.sent[0].get_content()


async def test_attachments_are_saved_with_the_subject(tmp_path):
    app = build(tmp_path)
    app.imap.add(make("见附件", subject="发票", files=[("p.png", "image/png", b"\x89PNG"),
                                                       ("a.pdf", "application/pdf", b"%PDF")]))
    await run(app)
    [entry] = entries(app)
    assert "发票" in entry.body and "图片文字" in entry.body and "PDF 文字" in entry.body
    assert len(app.smtp.sent) == 1


async def test_an_empty_mail_says_so(tmp_path):
    app = build(tmp_path)
    app.imap.add(make("", subject=""))
    await run(app)
    assert entries(app) == [] and app.smtp.sent[0].get_content().strip() == NOTHING_TO_SAVE
    assert app.smtp.sent[0]["Subject"] == "Octopus 存好了"


# ---- who may send ----

async def test_others_and_spoofed_mail_are_passed_over_with_one_line_each(tmp_path, caplog):
    app = build(tmp_path)
    caplog.set_level(logging.INFO)
    stranger = app.imap.add(make("https://a.com/x", sender="x@other.example"))
    app.imap.add(make("https://a.com/y", sender="x@other.example"))
    spoof = app.imap.add(make("https://a.com/z", auth="mx.google.com; dkim=fail header.d=example.com; spf=fail "
                                                       "smtp.mailfrom=me@example.com"))
    assert await run(app) == 0
    assert entries(app) == [] and app.smtp.sent == []
    assert app.imap.mails[stranger][1] == set() and app.imap.mails[spoof][1] == set()  # left unread
    lines = [r.getMessage() for r in caplog.records if " ignored: " in r.getMessage()]
    assert len(lines) == 2  # once per sender
    assert not any("other.example" in ln or OWNER in ln for ln in lines)
    assert app.ch.intake.last_uid(7) == spoof  # never looked at again


async def test_an_automatic_mail_or_our_own_is_never_answered(tmp_path):
    app = build(tmp_path)
    app.imap.add(make("https://a.com/x", extra_headers=[("Auto-Submitted", "auto-replied")]))
    app.ch.conf.owner_addresses.append(BOX)
    app.imap.add(make("https://a.com/y", sender=BOX, auth="mx.google.com; dkim=pass header.d=example.com"))
    assert await run(app) == 0 and app.smtp.sent == []


async def test_a_mail_too_big_is_passed_over(tmp_path, monkeypatch):
    import octopus.channels.email as mod

    app = build(tmp_path)
    monkeypatch.setattr(mod, "MAX_MAIL_BYTES", 100)
    app.imap.add(make("https://a.com/x"))
    assert await run(app) == 0 and entries(app) == []
    assert MAX_MAIL_BYTES == 40 * 1024 * 1024


# ---- nothing saved twice, nothing lost ----

async def test_the_first_poll_takes_the_unread_then_everything_after_the_mark(tmp_path):
    app = build(tmp_path)
    app.imap.add(make("https://old.example/read"), seen=True)  # read before Octopus looked: left alone
    app.imap.add(make("https://a.com/x"))
    assert await run(app) == 1
    assert app.imap.calls[1] == ("SEARCH", None, "UNSEEN")
    app.imap.add(make("https://b.com/y"), seen=True)  # read on the phone before the next poll: still taken
    assert await run(app) == 1
    assert ("SEARCH", None, "UID 3:*") in app.imap.calls
    assert await run(app) == 0  # "UID 4:*" names the last mail again: not taken twice
    assert len(entries(app)) == 2


async def test_a_restart_before_marking_read_does_not_save_twice(tmp_path):
    app = build(tmp_path)
    uid = app.imap.add(make("https://a.com/x"))
    await run(app)
    app.imap.mails[uid][1].clear()  # as if the \\Seen never reached the server
    fresh = EmailChannel(app.ch.conf, PASSWORD, imap_factory=lambda h, p: app.imap,
                         smtp_factory=lambda h, p: app.smtp)
    fresh.intake = MailIntake(app, app.ch.conf, fresh.send_mail)
    assert await fresh.intake.save(7, uid, None) is None  # the same (UIDVALIDITY, UID) twice: not taken
    assert await fresh.poll_once() == 0 and len(entries(app)) == 1


async def test_a_new_uidvalidity_starts_from_the_unread_again(tmp_path):
    app = build(tmp_path)
    app.imap.add(make("https://a.com/x"))
    await run(app)
    app.imap.uidvalidity = 8
    app.imap.add(make("https://b.com/y"))
    assert await run(app) == 1  # the first mail is \\Seen: only the new one
    assert app.imap.calls.count(("SEARCH", None, "UNSEEN")) == 2 and len(entries(app)) == 2


async def test_a_mailbox_that_cannot_be_read_is_logged_once(tmp_path, caplog):
    app = build(tmp_path)
    app.imap.fail_login = True
    caplog.set_level(logging.INFO)
    assert await app.ch.poll_once() == 0
    assert await app.ch.poll_once() == 0
    lines = [r.getMessage() for r in caplog.records if "cannot read the mailbox" in r.getMessage()]
    assert len(lines) == 1 and BOX not in lines[0] and PASSWORD not in lines[0]
    app.imap.fail_login = False
    await app.ch.poll_once()
    assert any("can be read again" in r.getMessage() for r in caplog.records)


# ---- replies ----

async def test_a_failed_reply_goes_out_later_once_whole(tmp_path):
    app = build(tmp_path)
    app.smtp.fail = True
    app.imap.add(make("https://a.com/x\nhttps://b.com/y"))
    await run(app)
    assert app.smtp.sent == [] and app.outbox.pending_users() == [("email", "mail-1")]
    app.smtp.fail = False
    await app.sweep_outbox()
    [reply] = app.smtp.sent
    assert reply.get_content().count("✅ 已保存") == 2
    assert app.outbox.pending_users() == []
    await app.sweep_outbox()
    assert len(app.smtp.sent) == 1


async def test_a_failed_reply_with_no_job_is_kept_too(tmp_path):
    app = build(tmp_path)
    app.smtp.fail = True
    app.imap.add(make("", subject=""))
    await app.ch.poll_once()
    app.smtp.fail = False
    await app.sweep_outbox()
    assert [m.get_content().strip() for m in app.smtp.sent] == [NOTHING_TO_SAVE]


async def test_replies_to_telegram(tmp_path):
    app = build(tmp_path, reply="telegram", telegram=True)
    app.imap.add(make("https://a.com/x"))
    await run(app)
    tg = app.channels["telegram"]
    assert app.smtp.sent == [] and len(tg.sent) == 1 and tg.sent[0][0] == "owner1"
    assert "✅ 已保存" in tg.sent[0][1]


async def test_replies_to_telegram_when_it_does_not_run_are_dropped_and_logged_once(tmp_path, caplog):
    app = build(tmp_path, reply="telegram")
    caplog.set_level(logging.INFO)
    app.imap.add(make("https://a.com/x"))
    app.imap.add(make("https://b.com/y"))
    await run(app)
    assert app.smtp.sent == []
    assert sum("Telegram is not running" in r.getMessage() for r in caplog.records) == 1


async def test_no_replies(tmp_path):
    app = build(tmp_path, reply="none")
    app.imap.add(make("https://a.com/x"))
    await run(app)
    assert app.smtp.sent == [] and len(entries(app)) == 1


async def test_a_failed_link_reply_says_how_to_add_more(tmp_path):
    app = build(tmp_path, fetchers=[StubFetcher(FetchResult(url="https://a.com/x", source="web",
                                                            status="needs_manual", note="要登录"))])
    app.imap.add(make("https://a.com/x"))
    await run(app)
    body = app.smtp.sent[0].get_content()
    assert "/add" in body and "请发截图" not in body


# ---- config and wiring ----

def test_config_email_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[channels.email]\nenabled = true\nimap_host = "imap.gmail.com"\nsmtp_host = "smtp.gmail.com"\n'
                 'username = "box@example.com"\nowner_addresses = ["Me@Example.com"]\nreply = "telegram"\n',
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg.email == EmailConfig(enabled=True, imap_host="imap.gmail.com", smtp_host="smtp.gmail.com",
                                    username="box@example.com", owner_addresses=["me@example.com"], reply="telegram")
    assert "email" not in cfg.channels  # never an owner-notice or chat channel


def test_off_by_default(tmp_path):
    cfg = load_config(tmp_path / "none.toml")
    assert cfg.email == EmailConfig() and not cfg.email.enabled and cfg.email.poll_seconds == 60


@pytest.mark.parametrize("body", [
    '[channels.email]\nauthserv_id = 5\n',
    '[channels.email]\nauthserv_id = ["mx.google.com", 1]\n',
    '[channels.email]\nenabled = true\n',  # nothing to read
    '[channels.email]\nenabled = true\nimap_host = "i"\nusername = "u@example.com"\nowner_addresses = []\n',
    '[channels.email]\nenabled = true\nimap_host = "i"\nusername = "u@example.com"\nowner_addresses = ["a@b.c"]\n'
    'reply = "email"\n',  # replies by email need smtp_host
    '[channels.email]\nreply = "sms"\n',
    '[channels.email]\npoll_seconds = 1\n',
    '[channels.email]\nimap_port = "993"\n',
    '[channels.email]\nowner_addresses = "a@b.c"\n',
    '[channels.email]\npassword = "x"\n',
])
def test_config_email_mistakes_are_told(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


class Secrets:
    def __init__(self, **values):
        self.values = values

    def get(self, name):
        return self.values.get(name)


def test_build_channels_needs_the_password(tmp_path):
    cfg = make_test_config(tmp_path)
    cfg.email = EmailConfig(enabled=True, imap_host="imap.gmail.com", smtp_host="s", username=BOX,
                            owner_addresses=[OWNER])
    channels, problems = build_channels(cfg, Secrets(), notify=None)
    assert channels == {} and "email_password" in problems[0]
    channels, problems = build_channels(cfg, Secrets(email_password="pw"), notify=None)
    assert isinstance(channels["email"], EmailChannel) and problems == []


async def test_start_polls_only_once_the_intake_is_there(tmp_path):
    import asyncio

    app = build(tmp_path)
    app.ch.conf.poll_seconds = 0.01
    app.imap.add(make("https://a.com/x"))
    task = asyncio.create_task(app.ch.start(None))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if app.ch.intake.last_uid(7):
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert app.ch.intake.last_uid(7) == 1


def test_real_openers_verify_the_server_certificate(monkeypatch):
    """C1: TLS with a verifying context (certificate and host name), from the start or by STARTTLS, with timeouts."""
    import ssl

    import octopus.channels.email as mod

    made = []

    def check(ctx):
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname

    def imap_ssl(h, p, ssl_context=None, timeout=None):
        check(ssl_context)
        made.append(("imap-ssl", h, p, timeout))

    def smtp_ssl(h, p, context=None, timeout=None):
        check(context)
        made.append(("smtp-ssl", h, p, timeout))

    class Plain:
        def __init__(self, h, p, timeout=None):
            made.append(("plain", h, p, timeout))

        def starttls(self, ssl_context=None, context=None):
            check(ssl_context or context)
            made.append("starttls")

    monkeypatch.setattr(mod.imaplib, "IMAP4_SSL", imap_ssl)
    monkeypatch.setattr(mod.smtplib, "SMTP_SSL", smtp_ssl)
    monkeypatch.setattr(mod.imaplib, "IMAP4", Plain)
    monkeypatch.setattr(mod.smtplib, "SMTP", Plain)
    mod._open_imap("i", 993)
    mod._open_imap("i", 143)
    mod._open_smtp("s", 465)
    mod._open_smtp("s", 587)
    t = mod.NETWORK_TIMEOUT_SECONDS
    assert made == [("imap-ssl", "i", 993, t), ("plain", "i", 143, t), "starttls", ("smtp-ssl", "s", 465, t),
                    ("plain", "s", 587, t), "starttls"]
    assert 0 < t <= 60


async def test_the_same_newsletter_twice_is_a_duplicate(tmp_path):
    app = build(tmp_path)
    for _ in range(2):
        app.imap.add(make(LONG_TEXT, NEWSLETTER_HTML, subject="Fwd: 本周通讯"))
        await run(app)
    assert len(entries(app)) == 1
    assert "已经存过了" in app.smtp.sent[1].get_content()


async def test_a_text_that_comes_while_the_reply_is_being_sent_is_not_lost(tmp_path):
    import asyncio

    app = build(tmp_path)
    app.imap.add(make("https://a.com/x"))
    await app.ch.poll_once()
    intake, gate, real = app.ch.intake, asyncio.Event(), app.ch.send_mail

    async def slow(*args):
        await gate.wait()
        await real(*args)

    intake.send_mail = slow
    first = asyncio.create_task(intake.reply("mail-1", "第一条"))
    await asyncio.sleep(0)
    app.jobs.complete(1, "x")
    first_done = asyncio.create_task(intake._flush(1))
    late = asyncio.create_task(intake.reply("mail-1", "后来的一条"))
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(first, first_done, late)
    bodies = [m.get_content() for m in app.smtp.sent]
    assert sum("后来的一条" in b for b in bodies) == 1 and sum("第一条" in b for b in bodies) == 1


def test_the_server_name_to_trust(tmp_path):
    """C2: Gmail's is built in; any other box needs authserv_id in the config, else the channel does not start."""
    from octopus.mail import trusted_servers

    assert trusted_servers(EmailConfig(imap_host="imap.gmail.com")) == ["mx.google.com"]
    assert trusted_servers(EmailConfig(imap_host="IMAP.googlemail.com ")) == ["mx.google.com"]
    assert trusted_servers(EmailConfig(imap_host="imap.qq.com")) == []
    assert trusted_servers(EmailConfig(imap_host="outlook.office365.com")) == []  # no server name in its header
    assert trusted_servers(EmailConfig(imap_host="imap.qq.com", authserv_id=["mx.qq.example"])) == ["mx.qq.example"]
    p = tmp_path / "config.toml"
    p.write_text('[channels.email]\nauthserv_id = "mx.example.net"\n', encoding="utf-8")
    assert load_config(p).email.authserv_id == ["mx.example.net"]


def test_a_box_whose_server_name_is_unknown_does_not_start(tmp_path):
    from octopus.mail import UNKNOWN_SERVER

    cfg = make_test_config(tmp_path)
    cfg.email = EmailConfig(enabled=True, imap_host="imap.qq.com", smtp_host="s", username=BOX,
                            owner_addresses=[OWNER])
    channels, problems = build_channels(cfg, Secrets(email_password="pw"), notify=None)
    assert channels == {} and problems == [UNKNOWN_SERVER.format(host="imap.qq.com")]
    cfg.email.authserv_id = ["mx.qq.example"]
    assert "email" in build_channels(cfg, Secrets(email_password="pw"), notify=None)[0]


async def test_a_mail_with_a_broken_from_is_skipped_once_and_the_box_goes_on(tmp_path, caplog):
    """I1: one malformed mail never stops the channel; it is passed over for good and logged once."""
    app = build(tmp_path)
    caplog.set_level(logging.INFO)
    poison = app.imap.add(b"From: .@,<=?utf-8?b?@[\t  \r\nTo: octopus@example.com\r\nSubject: hi\r\n\r\nspam\r\n")
    good = app.imap.add(make("https://a.com/x"))
    assert await run(app) == 1
    assert "\\Seen" in app.imap.mails[good][1] and len(entries(app)) == 1
    assert app.ch.intake.last_uid(7) == good
    await run(app)
    assert sum("reading the mailbox stopped" in r.getMessage() for r in caplog.records) == 0
    assert app.imap.calls.count(("FETCH", str(poison), "(RFC822.SIZE BODY.PEEK[HEADER])")) == 1


async def test_reading_and_planning_never_run_on_the_event_loop(tmp_path, monkeypatch):
    """I3: MIME / HTML parsing (a big newsletter takes seconds) runs in a thread."""
    import threading

    import octopus.channels.email as chmod
    import octopus.mail as mailmod

    app = build(tmp_path)
    threads = {}

    def spy(name, fn):
        def wrapped(*a, **k):
            threads[name] = threading.current_thread() is threading.main_thread()
            return fn(*a, **k)
        return wrapped

    monkeypatch.setattr(mailmod, "plan_mail", spy("plan", mailmod.plan_mail))
    monkeypatch.setattr(chmod, "read_mail", spy("read", chmod.read_mail))
    monkeypatch.setattr(chmod, "authenticated", spy("auth", chmod.authenticated))
    monkeypatch.setattr(chmod, "sender_of", spy("sender", chmod.sender_of))
    app.imap.add(make(LONG_TEXT, NEWSLETTER_HTML, subject="Fwd: 通讯"))
    await run(app)
    assert threads == {"plan": False, "read": False, "auth": False, "sender": False}


async def test_a_strangers_mail_is_never_downloaded_whole(tmp_path):
    """M3: the headers first; the body only for a mail that is taken."""
    app = build(tmp_path)
    stranger = app.imap.add(make("https://a.com/x", sender="x@other.example"))
    mine = app.imap.add(make("https://a.com/y"))
    await run(app)
    assert ("FETCH", str(stranger), "(BODY.PEEK[])") not in app.imap.calls
    assert ("FETCH", str(mine), "(BODY.PEEK[])") in app.imap.calls


async def test_a_failed_fetch_is_tried_again_next_poll(tmp_path):
    """I5: a transient NO to the body FETCH never moves the mark past the mail."""
    app = build(tmp_path)
    uid = app.imap.add(make("https://a.com/x"))
    app.imap.fail_fetch.add(uid)
    assert await run(app) == 0 and app.ch.intake.last_uid(7) is None
    assert app.imap.mails[uid][1] == set()
    app.imap.fail_fetch.clear()
    assert await run(app) == 1 and len(entries(app)) == 1


async def test_a_save_cut_off_by_a_restart_is_finished_after_it(tmp_path, monkeypatch):
    """I5: the mark moves only once every save of the mail is queued; a stop in the middle (staging a big
    attachment) leaves the mail to be taken again, and what was already queued is not queued twice."""
    import asyncio

    import octopus.mail as mailmod

    app = build(tmp_path)
    uid = app.imap.add(make("https://a.com/x", subject="看", files=[("p.png", "image/png", b"\x89PNG")]))
    real = mailmod.MailIntake._stage

    async def stopped(self, files):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mailmod.MailIntake, "_stage", stopped)
    with pytest.raises(asyncio.CancelledError):
        await app.ch.poll_once()
    assert app.ch.intake.last_uid(7) is None and app.imap.mails[uid][1] == set()
    assert app.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1  # the link was queued
    monkeypatch.setattr(mailmod.MailIntake, "_stage", real)
    fresh = EmailChannel(app.ch.conf, PASSWORD, imap_factory=lambda h, p: app.imap,
                         smtp_factory=lambda h, p: app.smtp)  # the restarted service
    fresh.intake = MailIntake(app, app.ch.conf, fresh.send_mail)
    app.channels["email"] = fresh
    assert await fresh.poll_once() == 1
    await app.process_due_jobs()
    kinds = [r[0] for r in app.conn.execute("SELECT kind FROM jobs ORDER BY id")]
    assert kinds == ["url", "attachments"]
    assert fresh.intake.last_uid(7) == uid and "\\Seen" in app.imap.mails[uid][1]
    [reply] = app.smtp.sent
    assert reply.get_content().count("✅ 已保存") == 2


async def test_a_reply_subject_with_a_line_break_still_goes(tmp_path):
    """M4: a decoded subject may hold CR/LF; the reply's subject is one line."""
    import base64

    app = build(tmp_path)
    enc = base64.b64encode("hi\r\nBcc: x@example.net".encode()).decode()
    m = make("https://a.com/x")
    raw = m.as_bytes().replace(b"Subject: =?utf-8?b?5Li76aKY?=", f"Subject: =?utf-8?b?{enc}?=".encode())
    assert raw != m.as_bytes()
    app.imap.add(raw)
    await run(app)
    [reply] = app.smtp.sent
    assert reply["Subject"] == "Re: hi Bcc: x@example.net" and reply["Bcc"] is None


async def test_a_hanging_smtp_server_frees_the_reply_lock(tmp_path, monkeypatch):
    """M8: an SMTP exchange that hangs before the mail is handed over gives up after a timeout (the reply is tried
    again later), and the late thread never sends it after that."""
    import asyncio
    import time

    import octopus.channels.email as mod

    monkeypatch.setattr(mod, "SMTP_TOTAL_SECONDS", 0.05)
    sent = []

    class SlowLogin(FakeSMTP):
        def login(self, user, password):
            time.sleep(0.2)

        def send_message(self, msg):
            sent.append(msg)

    app = build(tmp_path)
    app.ch.smtp_factory = lambda h, p: SlowLogin()
    with pytest.raises(TimeoutError):
        await app.ch.send_mail(OWNER, "Re: x", None, "body")
    await asyncio.sleep(0.4)
    assert sent == []  # the retry from the outbox is the only send


async def test_a_reply_that_times_out_while_being_handed_over_counts_as_sent(tmp_path, monkeypatch, caplog):
    """Re-review minor: once the mail is being handed to the server, a timeout is not a failure (a retry could send
    it twice); it is logged."""
    import time

    import octopus.channels.email as mod

    monkeypatch.setattr(mod, "SMTP_TOTAL_SECONDS", 0.05)
    caplog.set_level(logging.INFO)

    class SlowSend(FakeSMTP):
        def send_message(self, msg):
            time.sleep(0.2)

    app = build(tmp_path)
    app.ch.smtp_factory = lambda h, p: SlowSend()
    await app.ch.send_mail(OWNER, "Re: x", None, "body")  # no TimeoutError: not tried again
    assert any("may have been sent" in r.getMessage() for r in caplog.records)


# ---- fix round 2: a held mail never blocks the box (N2) ----

def _held_box(app, stuck):
    """One mail whose save was cut off (its row left open), 60 mails done with after it, 5 new ones."""
    from octopus.mail import read_mail

    saves = app.ch.intake.saves
    zero = app.imap.add(make("earlier", subject="z"), seen=True)
    saves.update(saves.add(7, zero, read_mail(make("x", subject="z"))), state="replied")
    first = app.imap.add(make("a note body that is long enough to be a note " * 3, subject="first"))
    saves.add(7, first, read_mail(make("x", subject="first")))  # left 'open' by a stop mid-save
    for i in range(60):
        u = app.imap.add(make(f"old {i} " * 10, subject=f"o{i}"), seen=True)
        saves.update(saves.add(7, u, read_mail(make("x", subject="o"))), state="replied")
    news = [app.imap.add(make(f"note number {i} " * 10, subject=f"n{i}")) for i in range(5)]
    stuck(app, first)
    return first, news


@pytest.mark.parametrize("label", ["gone", "not an owner now", "fails for good"])
async def test_a_held_mail_that_is_gone_or_rejected_never_blocks_new_mail(tmp_path, caplog, label):
    def stuck(app, uid):
        if label == "gone":  # archived or deleted in Gmail before the restart
            del app.imap.mails[uid]
        elif label == "not an owner now":  # owner_addresses changed before the restart
            app.imap.mails[uid][0] = make("x", sender="someone@other.example").as_bytes()
        else:
            app.imap.fail_fetch.add(uid)

    app = build(tmp_path)
    caplog.set_level(logging.INFO)
    first, news = _held_box(app, stuck)
    now = [0.0]
    app.ch.clock = lambda: now[0]
    for _ in range(4):
        await run(app)
        now[0] += 300  # polls 5 minutes apart: the 4th refusal is past the 10-minute floor
    assert all("\\Seen" in app.imap.mails[u][1] for u in news), "all 5 new mails are taken"
    assert app.ch.intake.saves.find(7, first)["state"] != "open"
    assert app.ch.intake.last_uid(7) == news[-1]
    dropped = [r.getMessage() for r in caplog.records if "unfinished save" in r.getMessage()]
    assert len(dropped) == 1
    # mails done with are never downloaded again
    bodies = [c for c in app.imap.calls if c[0] == "FETCH" and c[2] == "(BODY.PEEK[])"]
    assert len(bodies) <= 5 + 3


async def test_a_mail_whose_fetch_keeps_failing_is_passed_over_after_three_tries_and_ten_minutes(tmp_path, caplog):
    """Fix round 3: 3 refusals are not enough on their own (a short outage); 10 minutes since the first one too. The
    owner is told once, on Telegram when replies go by email (the box may be what fails)."""
    import octopus.channels.email as mod

    app = build(tmp_path, telegram=True)
    caplog.set_level(logging.INFO)
    now = [1000.0]
    app.ch.clock = lambda: now[0]
    bad = app.imap.add(make("https://a.com/x", subject="一封很长很长很长很长很长很长的周刊标题，超过二十个字"))
    good = app.imap.add(make("https://b.com/y"))
    app.imap.fail_fetch.add(bad)
    for _ in range(mod.FETCH_ATTEMPTS + 2):  # five refusals within a minute: still waiting
        await run(app)
        now[0] += 10
    assert app.ch.intake.last_uid(7) is None and app.imap.mails[good][1] == set()
    now[0] = 1000.0 + mod.REFUSAL_WAIT_SECONDS
    await run(app)  # the next refusal, 10 minutes after the first: passed over
    await run(app)
    assert "\\Seen" in app.imap.mails[good][1] and app.imap.mails[bad][1] == set()
    assert app.ch.intake.last_uid(7) == good
    assert sum("could not be fetched" in r.getMessage() for r in caplog.records) == 1
    tg = app.channels["telegram"]
    told = [t for _, t in tg.sent if t.startswith("有一封邮件读不出来")]
    assert told == ["有一封邮件读不出来，已跳过：一封很长很长很长很长很长很长的周刊标题，…"]  # the first 20 characters
    assert mod.FETCH_ATTEMPTS == 3 and mod.REFUSAL_WAIT_SECONDS == 600


@pytest.mark.parametrize("reply", ["telegram", "none"])
async def test_the_skip_notice_follows_the_reply_setting(tmp_path, reply):
    import octopus.channels.email as mod

    app = build(tmp_path, reply=reply, telegram=True)
    now = [0.0]
    app.ch.clock = lambda: now[0]
    bad = app.imap.add(make("https://a.com/x", subject="周刊"))
    app.imap.fail_fetch.add(bad)
    for _ in range(mod.FETCH_ATTEMPTS):
        await run(app)
        now[0] += mod.REFUSAL_WAIT_SECONDS
    told = [t for _, t in app.channels["telegram"].sent if t.startswith("有一封邮件读不出来")]
    assert told == (["有一封邮件读不出来，已跳过：周刊"] if reply == "telegram" else [])
    assert app.smtp.sent == []  # never by the box


async def test_the_size_is_found_wherever_the_server_puts_it(tmp_path):
    app = build(tmp_path)
    app.imap.size_last = True
    app.imap.add(make("https://a.com/x"))
    assert await run(app) == 1


def test_an_email_saves_table_without_done_gets_it(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE email_saves(id INTEGER PRIMARY KEY AUTOINCREMENT, uidvalidity INTEGER NOT NULL, "
                "uid INTEGER NOT NULL, sender TEXT, subject TEXT NOT NULL, message_id TEXT, "
                "jobs TEXT NOT NULL DEFAULT '[]', replies TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL, "
                "created_at REAL NOT NULL, UNIQUE(uidvalidity, uid))")
    old.execute("INSERT INTO email_saves(uidvalidity, uid, subject, state, created_at) VALUES (7, 1, '', 'open', 0)")
    old.commit()
    old.close()
    for _ in range(2):  # idempotent
        conn = connect(db, embed_dim=FakeEmbedder.dim)
        assert conn.execute("SELECT done FROM email_saves").fetchone()[0] == "[]"
        conn.close()


# ---- P25 (int2 Q1, review m9 / Q-a): dmarc=temperror: dkim / spf decide; not trusted: passed over, owner told ----

TEMPERROR = "mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=me@example.com; " \
            "dmarc=temperror header.from=example.com"
TEMPERROR_UNALIGNED = "mx.google.com; dkim=pass header.d=other.example; spf=fail smtp.mailfrom=me@example.com; " \
                      "dmarc=temperror header.from=example.com"


async def test_a_temporary_dmarc_error_with_aligned_dkim_is_saved(tmp_path):
    app = build(tmp_path, telegram=True)
    app.imap.add(make("https://a.com/x", auth=TEMPERROR))
    assert await run(app) == 1 and len(entries(app)) == 1 and app.channels["telegram"].sent == []


async def test_a_temporary_dmarc_error_without_alignment_is_passed_over_and_told_once(tmp_path, caplog):
    app = build(tmp_path, telegram=True)
    caplog.set_level(logging.INFO)
    uid = app.imap.add(make("https://a.com/x", subject="周报", auth=TEMPERROR_UNALIGNED))
    assert await run(app) == 0 and await run(app) == 0
    assert entries(app) == [] and app.smtp.sent == []  # never answered by mail: the sender is not proven
    assert app.imap.mails[uid][1] == set()  # left unread
    assert app.ch.intake.last_uid(7) == uid  # recorded: polls move on, however many such mails come
    told = app.channels["telegram"].sent
    assert told == [("owner1", "邮件服务商暂时没能验证一封邮件是不是你发的，先没有存：周报。过一会儿再发一次就好。")]
    assert sum("dmarc=temperror" in r.getMessage() for r in caplog.records) == 1


async def test_the_resend_notice_is_not_repeated_after_a_restart(tmp_path):
    app = build(tmp_path, telegram=True)
    app.imap.add(make("https://a.com/x", auth=TEMPERROR_UNALIGNED))
    await run(app)
    fresh = EmailChannel(app.cfg.email, PASSWORD, imap_factory=lambda h, p: app.imap, smtp_factory=lambda h, p: app.smtp)
    fresh.intake = app.ch.intake
    app.ch = fresh
    assert await run(app) == 0 and len(app.channels["telegram"].sent) == 1


async def test_a_permanent_dmarc_error_is_still_passed_over_silently(tmp_path):
    app = build(tmp_path, telegram=True)
    uid = app.imap.add(make("https://a.com/x", auth=TEMPERROR_UNALIGNED.replace("temperror", "permerror")))
    assert await run(app) == 0
    assert app.ch.intake.last_uid(7) == uid and app.channels["telegram"].sent == []


async def test_a_temporary_dmarc_error_is_not_told_when_replies_are_off(tmp_path):
    app = build(tmp_path, reply="none", telegram=True)
    app.imap.add(make("https://a.com/x", auth=TEMPERROR_UNALIGNED))
    assert await run(app) == 0 and app.channels["telegram"].sent == []
