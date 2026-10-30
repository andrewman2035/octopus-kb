"""P23: reading a mail for the save-only email channel: who really sent it, and what in it is saved how."""
from email.message import EmailMessage

import pytest

from octopus.mail import (ARTICLE, FILES, LINKS, NOTE, Mail, authenticated, plan_mail, read_mail,
                          view_online_link)

import email
from email import policy

OWNER = "me@example.com"
LONG_TEXT = "长期主义的投资者相信复利，时间是最好的朋友。" * 20
NEWSLETTER_HTML = (
    "<html><body><p><a href='https://news.example.com/p/1?utm_source=email'>View in browser</a></p>"
    f"<article><h1>本周通讯</h1><p>{LONG_TEXT}</p></article>"
    "<p><a href='https://news.example.com/a'>a</a> <a href='https://news.example.com/b'>b</a>"
    " <a href='https://news.example.com/unsubscribe'>Unsubscribe</a></p></body></html>")


def make(text=None, html=None, sender=f"Me <{OWNER}>", subject="主题", auth="mx.google.com; dkim=pass "
         "header.i=@example.com header.s=s1 header.b=abc; spf=pass (google.com: domain of me@example.com designates "
         "1.2.3.4 as permitted sender) smtp.mailfrom=me@example.com; dmarc=pass header.from=example.com",
         files=(), extra_headers=()) -> EmailMessage:
    m = EmailMessage()
    for name, value in extra_headers:
        m[name] = value
    if auth is not None:
        m["Authentication-Results"] = auth
    m["From"] = sender
    m["To"] = "octopus@example.com"
    m["Subject"] = subject
    m["Message-ID"] = "<abc@example.com>"
    if text is not None:
        m.set_content(text)
    if html is not None:
        if text is None:
            m.set_content(html, subtype="html")
        else:
            m.add_alternative(html, subtype="html")
    for name, mime, data in files:
        major, minor = mime.split("/")
        m.add_attachment(data, maintype=major, subtype=minor, filename=name)
    return m


# ---- who sent it ----

TRUSTED = ["mx.google.com"]


def test_the_providers_dkim_pass_for_the_owners_domain_is_trusted():
    assert authenticated(make("x"), OWNER, TRUSTED)


@pytest.mark.parametrize("auth", [
    "mx.google.com; dkim=pass header.i=@other.example header.s=s; spf=fail smtp.mailfrom=me@example.com",
    "mx.google.com; dkim=fail header.d=example.com; spf=softfail smtp.mailfrom=me@example.com; dmarc=fail",
    "mx.google.com; dkim=none; spf=none smtp.mailfrom=me@example.com",
    None,
])
def test_anything_else_is_not(auth):
    assert not authenticated(make("x", auth=auth), OWNER, TRUSTED)


def test_spf_pass_for_the_domain_is_enough():
    assert authenticated(make("x", auth="mx.google.com; spf=pass smtp.mailfrom=me@example.com"), OWNER, TRUSTED)


def test_only_a_header_from_the_trusted_server_is_read():
    """C2: the receiving provider's header is found by its server name, never by its place."""
    assert not authenticated(make("x", auth="mx.example.net; dkim=pass header.d=example.com"), OWNER, TRUSTED)
    assert authenticated(make("x", auth="mx.example.net; dkim=pass header.d=example.com"), OWNER, ["MX.example.net"])
    assert not authenticated(make("x"), OWNER, [])  # nothing trusted: nothing taken


def test_a_header_without_a_server_name_is_never_trusted():
    """Outlook writes its header with no server name: it cannot be told from one the sender wrote."""
    auth = ("spf=pass (sender IP is 1.2.3.4) smtp.mailfrom=example.com; dkim=pass (signature was verified) "
            "header.d=example.com;dmarc=pass action=none header.from=example.com;compauth=pass reason=100")
    assert not authenticated(make("x", auth=auth), OWNER, TRUSTED)


def test_a_forged_header_with_no_provider_header_is_rejected():
    """The provider stamped nothing (a box that writes no header, or a mail APPENDed into it): the sender's own
    header, whatever server name it claims, is not the provider's."""
    assert not authenticated(make("x", auth="evil.example; dkim=pass header.d=example.com"), OWNER, TRUSTED)


def test_a_forged_header_below_the_providers_is_not_read():
    """The sender can write an Authentication-Results header too, even with the provider's name; the provider's own
    is added on top, and only the topmost one with the trusted name is read."""
    m = make("x", auth="mx.google.com; dkim=fail header.d=example.com; spf=fail smtp.mailfrom=me@example.com")
    m["Authentication-Results"] = "mx.google.com; dkim=pass header.d=example.com"  # appended below: the forged one
    assert not authenticated(m, OWNER, TRUSTED)
    m = make("x", auth="other.example; dkim=pass header.d=example.com")  # a foreign server name on top
    m["Authentication-Results"] = "mx.google.com; dkim=fail header.d=example.com"
    assert not authenticated(m, OWNER, TRUSTED)


def test_a_pass_in_a_comment_is_not_a_pass():
    assert not authenticated(make("x", auth="mx.google.com; spf=fail (dkim=pass header.d=example.com) "
                                            "smtp.mailfrom=me@example.com"), OWNER, TRUSTED)
    assert not authenticated(make("x", auth="mx.google.com; spf=fail (a (nested) comment; dkim=pass "
                                            "header.d=example.com) smtp.mailfrom=me@example.com"), OWNER, TRUSTED)


@pytest.mark.parametrize("auth", [
    'mx.google.com; spf=pass (google.com: domain of x) smtp.mailfrom="x;dkim=pass header.d=example.com"@evil.test',
    'mx.google.com; dkim=fail reason="x; dkim=pass header.d=example.com"',
    'mx.google.com; spf=pass smtp.mailfrom="me"@example.com',  # a quoted value never counts
    'mx.google.com; dkim=fail reason="a \\" ; dkim=pass header.d=example.com"',  # an escaped quote stays inside
])
def test_a_pass_forged_inside_a_quoted_value_is_not_a_pass(auth):
    """I2: a ";" inside a quoted string does not start a new result."""
    assert not authenticated(make("x", auth=auth), OWNER, TRUSTED)


def test_the_header_tokenizer():
    from octopus.mail import parse_auth_results

    server, results = parse_auth_results(
        'mx.google.com 1; dkim=pass (good (very)) header.i=@example.com header.b="ab;c"; '
        'spf=pass smtp.mailfrom=me@example.com; dmarc=pass header.from=example.com')
    assert server == "mx.google.com"
    assert [(r.method, r.result) for r in results] == [("dkim", "pass"), ("spf", "pass"), ("dmarc", "pass")]
    assert results[0].props == {"header.i": "@example.com"} and results[0].quoted
    assert results[1].props == {"smtp.mailfrom": "me@example.com"} and not results[1].quoted
    assert parse_auth_results("spf=pass smtp.mailfrom=a.com")[0] is None  # no server name


@pytest.mark.parametrize("signer,sender,ok", [
    ("example.com", "me@example.com", True),
    ("example.com", "me@mail.example.com", True),  # the From is a subdomain of the signer's domain
    ("mail.example.com", "me@example.com", False),  # never the other way round
    ("notexample.com", "me@example.com", False),
    ("com", "me@example.com", False),  # never a public suffix
    ("com.cn", "me@example.com.cn", False),
    ("co.uk", "me@example.co.uk", False),
    ("example.co.uk", "me@news.example.co.uk", True),
])
def test_alignment(signer, sender, ok):
    """M2: relaxed, without the Public Suffix List: equal, or the From under the signer's domain, never a suffix."""
    m = make("x", sender=sender, auth=f"mx.google.com; dkim=pass header.d={signer}")
    assert authenticated(m, sender, TRUSTED) is ok


# ---- what is in it ----

def test_read_plain_and_html():
    mail = read_mail(make("正文 https://a.com/x", NEWSLETTER_HTML, subject="Fwd: 本周通讯"))
    assert (mail.sender, mail.sender_name, mail.subject, mail.message_id) == (OWNER, "Me", "本周通讯",
                                                                               "<abc@example.com>")
    assert mail.text.strip() == "正文 https://a.com/x" and "本周通讯" in mail.html and mail.files == []


def test_html_only_mail_has_its_text_read_from_the_html():
    mail = read_mail(make(None, NEWSLETTER_HTML))
    assert LONG_TEXT[:20] in mail.text and mail.html_only


def test_attachments_but_not_inline_pictures():
    m = make("看附件", files=[("论文.pdf", "application/pdf", b"%PDF"), ("p.png", "image/png", b"png")])
    m.get_payload()[0].add_related(b"inline", maintype="image", subtype="png", cid="<logo>")  # an HTML logo
    mail = read_mail(m)
    assert [(f.filename, f.mime, f.data) for f in mail.files] == [("论文.pdf", "application/pdf", b"%PDF"),
                                                                 ("p.png", "image/png", b"png")]


def test_the_forwarded_mails_sender_is_the_original_one():
    text = ("看看这个\n\n---------- Forwarded message ---------\nFrom: Daily News <news@news.example.com>\n"
            "Date: Mon, 1 Jan 2026\nSubject: 本周通讯\nTo: <me@example.com>\n\n" + LONG_TEXT)
    mail = read_mail(make(text, NEWSLETTER_HTML, subject="Fwd: 本周通讯"))
    assert mail.forwarded and mail.original_sender == "Daily News"
    mail = read_mail(make("---------- 转发的邮件 ----------\n发件人：周刊 <a@b.example>\n\n" + LONG_TEXT, subject="转发：周刊"))
    assert mail.original_sender == "周刊" and mail.subject == "周刊"


def test_view_in_browser_link():
    assert view_online_link(NEWSLETTER_HTML) == "https://news.example.com/p/1"
    assert view_online_link("<a href='https://x.example/v'>在浏览器中查看</a>") == "https://x.example/v"
    assert view_online_link("<a href='javascript:x'>View online</a><a href='https://a/b'>read</a>") is None
    assert view_online_link("<p>no links</p>") is None


# ---- what is saved how ----

def test_a_mail_with_just_a_link_saves_the_link():
    plan = plan_mail(read_mail(make("https://a.com/x?utm_source=t\n\nSent from my iPhone")))
    assert plan.kind == LINKS and plan.links == ["https://a.com/x"]


def test_a_forwarded_link_with_its_header_is_still_a_link_save():
    text = ("---------- Forwarded message ---------\nFrom: A <a@b.example>\nDate: Mon, 1 Jan 2026\n"
            "Subject: 看这个\nTo: <me@example.com>\n\nhttps://a.com/x\nhttps://b.com/y")
    plan = plan_mail(read_mail(make(text)))
    assert plan.kind == LINKS and plan.links == ["https://a.com/x", "https://b.com/y"]


def test_a_newsletter_is_an_article_from_its_html():
    plan = plan_mail(read_mail(make(LONG_TEXT + " https://news.example.com/a", NEWSLETTER_HTML, subject="Fwd: 通讯")))
    assert plan.kind == ARTICLE and plan.url == "https://news.example.com/p/1" and plan.title == "通讯"


def test_an_html_only_mail_is_an_article():
    plan = plan_mail(read_mail(make(None, NEWSLETTER_HTML)))
    assert plan.kind == ARTICLE


def test_a_typed_mail_is_a_note_even_with_its_html_copy():
    """Mail apps send what you type as HTML too: plain words with no newsletter about them are a note."""
    html = f"<div dir='ltr'>{LONG_TEXT}</div>"
    plan = plan_mail(read_mail(make(LONG_TEXT, html, subject="想法")))
    assert plan.kind == NOTE and plan.text.strip() == LONG_TEXT and plan.title == "想法" and not plan.links_unread


def test_a_note_with_links_says_they_were_not_read():
    plan = plan_mail(read_mail(make("一段自己的想法，比较长一些，" * 10 + "参考 https://a.com/x")))
    assert plan.kind == NOTE and plan.links_unread


def test_attachments_alone_are_saved_with_the_subject_and_text_as_caption():
    plan = plan_mail(read_mail(make("就这些", subject="发票", files=[("a.pdf", "application/pdf", b"%PDF")])))
    assert plan.kind == FILES and plan.caption == "发票\n就这些" and len(plan.files) == 1


def test_attachments_beside_a_link_go_with_the_subject_only():
    plan = plan_mail(read_mail(make("https://a.com/x", subject="看", files=[("p.png", "image/png", b"png")])))
    assert plan.kind == LINKS and plan.caption == "看" and len(plan.files) == 1


def test_an_empty_mail_has_nothing_to_save():
    plan = plan_mail(read_mail(make("", subject="")))
    assert plan.kind is None and not plan.files


def test_too_many_links_are_capped():
    plan = plan_mail(read_mail(make("\n".join(f"https://a.com/{i}" for i in range(30)))))
    assert plan.kind == LINKS and len(plan.links) == 10


def test_mail_is_a_plain_dataclass():
    assert Mail.__dataclass_fields__  # what the channel hands the intake


def _round_trip(m):
    return email.message_from_bytes(m.as_bytes(), policy=policy.default)


def test_a_mail_forwarded_as_an_attachment_is_read_as_the_forward():
    """I4: Outlook's / Gmail's "forward as attachment": the attached mail is the newsletter, never a 0-byte file."""
    inner = make(LONG_TEXT, NEWSLETTER_HTML, sender="Daily News <news@news.example.com>", subject="本周通讯", auth=None)
    outer = make("看看这个", subject="Fwd: 本周通讯")
    outer.add_attachment(inner)
    mail = read_mail(_round_trip(outer))
    assert mail.forwarded and mail.original_sender == "Daily News" and mail.files == []
    assert "本周通讯" in mail.html and LONG_TEXT[:20] in mail.text and "看看这个" in mail.text
    plan = plan_mail(mail)
    assert plan.kind == ARTICLE and plan.url == "https://news.example.com/p/1" and plan.title == "本周通讯"


def test_an_attached_plain_mail_is_read_too():
    inner = make("https://a.com/x", sender="A <a@b.example>", subject="链接", auth=None)
    outer = make("", subject="")
    outer.add_attachment(inner)
    mail = read_mail(_round_trip(outer))
    assert plan_mail(mail).links == ["https://a.com/x"] and mail.subject == "链接"


def test_an_empty_attachment_is_never_a_file():
    mail = read_mail(make("x", files=[("empty.txt", "text/plain", b"")]))
    assert mail.files == []


def test_unsubscribe_and_tracking_links_are_never_saved():
    """M1: a link the mail names in List-Unsubscribe, one whose text says unsubscribe (also 退订), one on such a line, or one
    whose path says so, is never fetched."""
    text = ("New post: https://blog.example.com/p/1\n\nUnsubscribe: https://news.example.com/u?u=123\n"
            "退订 https://news.example.com/x/9\nhttps://news.example.com/unsubscribe/abc\n"
            "https://news.example.com/list-unsub-1 https://t.example.com/track/open/1")
    html = ("<p><a href='https://blog.example.com/p/1'>New post</a>"
            "<a href='https://news.example.com/z'>Manage preferences</a></p>")
    m = make(text + "\nhttps://news.example.com/z\nhttps://news.example.com/hdr", html, extra_headers=[
        ("List-Unsubscribe", "<mailto:u@news.example.com>, <https://news.example.com/hdr>")])
    plan = plan_mail(read_mail(m))
    assert plan.kind == LINKS and plan.links == ["https://blog.example.com/p/1"]


# ---- fix round 2: the header reader always ends (N1) ----

STRAY = [
    "mx.google.com; dkim=pass )",
    "x; dkim=pass )",
    'mx.google.com; spf=pass (google.com: domain of "a)b"@evil.test designates 1.2.3.4 as permitted sender) '
    'smtp.mailfrom="a)b"@evil.test',
    ")))))", "(((((", '"""""', '\\', "(\\", '"\\', "a=(", "=;=;=", ")(", "mx.google.com; dkim=pass header.d=(",
]


def test_stray_parens_and_quotes_end_in_a_separate_process():
    """Checked in a child process first: a reader that never ends must fail this test, not hang the suite."""
    import subprocess
    import sys

    code = ("import json, sys\nfrom octopus.mail import parse_auth_results\n"
            "for v in json.load(sys.stdin): parse_auth_results(v)\nprint('ok')")
    import json as _json

    done = subprocess.run([sys.executable, "-c", code], input=_json.dumps(STRAY), capture_output=True, text=True,
                          timeout=20)
    assert done.stdout.strip() == "ok", done.stderr[-500:]


def test_a_stray_paren_is_a_literal():
    from octopus.mail import parse_auth_results

    server, results = parse_auth_results("mx.google.com; dkim=pass header.d=example.com )")
    assert server == "mx.google.com" and results[0].props["header.d"] == "example.com"


def test_random_and_adversarial_headers_finish_fast_and_never_raise():
    import random
    import time

    from octopus.mail import MAX_AR_CHARS, MAX_AR_TOKENS, _ar_tokens, parse_auth_results

    rng = random.Random(23)
    alphabet = '()";=\\ \t.@-_abcdkimspfpassé中\r\n'
    cases = list(STRAY) + ["(" * 5000, ")" * 5000, '"' * 5000, "\\" * 5000, "a=" * 4000, ";" * 8000,
                           "mx.google.com; " + "dkim=pass " * 2000, "(" * 3000 + ")" * 3000]
    cases += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 3000))) for _ in range(400)]
    cases += [rng.choice(STRAY) * rng.randint(1, 400) for _ in range(50)]
    for value in cases:
        start = time.perf_counter()
        try:
            parse_auth_results(value)
        except ValueError:
            pass  # too long or too many tokens: authenticated() reads it as not authenticated
        assert time.perf_counter() - start < 0.05, repr(value[:80])
        assert len(_ar_tokens(value[:MAX_AR_CHARS])) <= MAX_AR_TOKENS + 1
    m = make("x", auth="mx.google.com; " + "dkim=pass header.d=example.com " * 1000)  # over the cap
    assert not authenticated(m, OWNER, TRUSTED)
    assert not authenticated(make("x", auth="mx.google.com; dkim=pass )"), OWNER, TRUSTED)



# ---- fix round 3: the provider's header must look like one the provider wrote (Q3) ----

INJECTED = '"a) ; dkim=pass header.d=example.net ; (x"'
ESCAPED = '"a\\) ; dkim=pass header.d=example.net ; \\(x"'


@pytest.mark.parametrize("comment_part", [INJECTED, ESCAPED])
def test_an_envelope_sender_copied_into_the_providers_comment_cannot_inject_a_pass(comment_part, caplog):
    import logging

    auth = (f"mx.google.com; spf=pass (google.com: domain of {comment_part}@evil.test designates 1.2.3.4 as "
            f"permitted sender) smtp.mailfrom={INJECTED}@evil.test")
    m = make("x", sender="owner@example.net", auth=auth)
    caplog.set_level(logging.INFO)
    assert not authenticated(m, "owner@example.net", TRUSTED)
    assert sum("not trusted" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("auth", [
    "mx.google.com; dkim=pass header.d=example.com )",  # a ) that closes nothing
    "mx.google.com; dkim=pass (unclosed header.d=example.com",
    'mx.google.com; dkim=pass header.d=example.com header.b="x"',
    "mx.google.com; dkim=pass header.d=example.com header.b=a\\b",
])
def test_quotes_backslashes_or_unbalanced_parens_make_it_untrusted(auth):
    assert not authenticated(make("x", auth=auth), OWNER, TRUSTED)


def test_a_real_looking_gmail_header_still_passes():
    auth = ("mx.google.com; dkim=pass header.i=@example.com header.s=20230601 header.b=AbC1+/xy; spf=pass "
            "(google.com: domain of me@example.com designates 209.85.220.41 as permitted sender) "
            "smtp.mailfrom=me@example.com; dmarc=pass (p=NONE sp=QUARANTINE dis=NONE) header.from=example.com")
    assert authenticated(make("x", auth=auth), OWNER, TRUSTED)


# ---- integration hardening: a dmarc result decides; dkim / spf only when there is none ----

# the P23 reviewer's probe (probe_semicolon_unquoted.py): a provider that copies a bare ";" from a value unquoted
PROBE = ("mx.google.com; dkim=pass header.i=x; dkim=pass header.d=example.net x@evil.test header.s=s1 header.b=AbC1; "
         "spf=pass (google.com: domain of a@evil.test designates 1.2.3.4 as permitted sender) smtp.mailfrom=a@evil.test")
DMARC_FAIL = "; dmarc=fail (p=NONE sp=QUARANTINE dis=NONE) header.from=example.net"


@pytest.mark.parametrize("auth", [
    PROBE + DMARC_FAIL,  # the injected dkim=pass no longer outweighs the provider's own dmarc=fail
    PROBE.replace("header.i=x;", "header.i=x; dmarc=pass header.from=example.net x;") + DMARC_FAIL,  # two dmarcs
])
def test_an_injected_pass_cannot_outweigh_or_add_to_the_dmarc_result(auth):
    assert not authenticated(make("x", sender="owner@example.net", auth=auth), "owner@example.net", TRUSTED)


@pytest.mark.parametrize("auth", [
    "mx.google.com; dkim=pass header.d=example.com; dmarc=fail header.from=example.com",
    "mx.google.com; spf=pass smtp.mailfrom=me@example.com; dmarc=none header.from=example.com",
    "mx.google.com; dkim=pass header.d=example.com; dmarc=pass header.from=example.com; dmarc=pass header.from=example.com",
    "mx.google.com; dkim=pass header.d=example.com; dmarc=pass header.from=mail.example.com",  # not the From's domain
    "mx.google.com; dmarc=pass header.from=sub.example.com",
    "mx.google.com; dkim=pass header.d=example.com; dmarc=pass",  # no header.from
])
def test_with_a_dmarc_result_only_one_pass_for_the_from_domain_counts(auth):
    assert not authenticated(make("x", auth=auth), OWNER, TRUSTED)


def test_one_dmarc_pass_for_the_from_domain_is_enough_whatever_dkim_and_spf_say():
    auth = ("mx.google.com; dkim=fail header.d=example.com; spf=softfail smtp.mailfrom=me@example.com; "
            "dmarc=pass (p=REJECT) header.from=EXAMPLE.com")
    assert authenticated(make("x", auth=auth), OWNER, TRUSTED)


def test_without_any_dmarc_result_dkim_or_spf_alignment_still_counts():
    assert authenticated(make("x", auth="mx.google.com; dkim=pass header.d=example.com"), OWNER, TRUSTED)
    assert authenticated(make("x", sender="me@news.example.com",
                              auth="mx.google.com; dkim=pass header.d=example.com"), "me@news.example.com", TRUSTED)


# ---- P25 (int2 Q1, review m9 / Q-a): dmarc=temperror falls back to dkim / spf like a domain without DMARC ----

def test_a_temporary_dmarc_error_falls_back_to_dkim_and_spf_alignment():
    from octopus.mail import dmarc_temporary

    aligned = make("x", auth="mx.google.com; dkim=pass header.d=example.com; dmarc=temperror header.from=example.com")
    unaligned = make("x", auth="mx.google.com; dkim=pass header.d=other.example; spf=fail smtp.mailfrom=me@example.com; "
                               "dmarc=temperror header.from=example.com")
    perm = make("x", auth="mx.google.com; dkim=pass header.d=example.com; dmarc=permerror header.from=example.com")
    fail = make("x", auth="mx.google.com; dkim=pass header.d=example.com; dmarc=fail header.from=example.com")
    other = make("x", auth="mx.google.com; dkim=pass header.d=example.com; dmarc=temperror header.from=elsewhere.example")
    assert authenticated(aligned, OWNER, ["mx.google.com"])  # the same trust as a domain with no DMARC record
    for msg in (unaligned, perm, fail, other):
        assert not authenticated(msg, OWNER, ["mx.google.com"])
    assert dmarc_temporary(unaligned, OWNER, ["mx.google.com"])  # not trusted, but the provider's passing trouble
    assert not any(dmarc_temporary(m, OWNER, ["mx.google.com"]) for m in (perm, fail, other, make("x")))
    assert not dmarc_temporary(unaligned, OWNER, ["mx.other.example"])  # only the provider's own header counts
