"""P17: what the Chrome extension sends (a page, a selection, an image) is saved as a normal job; GET /recent's list."""
import base64
import dataclasses
import json
import logging

import httpx
import pytest
from fakes import FakeChannel, FakeEmbedder, FakeProvider, StubFetcher, fake_media, summary_json
from test_pipeline_page import ARTICLE_HTML, EMPTY_HTML, SHORT_HTML, WebStub

from octopus.app import App
from octopus.channels.base import user_tag
from octopus.channels.extension import ExtensionChannel
from octopus.config import ChannelConfig, make_test_config
from octopus.db import connect
from octopus.extension import (BAD_IMAGE, BAD_KIND, DONE, ENTRY_GONE, FAILED, NO_IMAGE, NO_SELECTION, NOT_A_WEB_PAGE,
                               NOTHING_READ, PENDING, Intake, parse_request, short_reason)
from octopus.fetchers import FetcherRegistry
from octopus.fetchers.base import FetchResult
from octopus.index import Index
from octopus.library import Library
from octopus.llm.router import LLMRouter
from octopus.models import IncomingMessage
from octopus.settings import Settings

URL = "https://a.com/x"
PNG = b"\x89PNG\r\n\x1a\nfake"


def build(tmp_path, fetchers=None, telegram=True, library_folder=None):
    cfg = make_test_config(tmp_path)
    cfg.library_folder = library_folder
    cfg.channels["tg"] = ChannelConfig(enabled=True, allowed_users=["me"])
    channels = {"extension": ExtensionChannel("token", port=0)}  # not started: its replies only
    tg = FakeChannel("telegram")
    if telegram:
        cfg.channels["telegram"] = ChannelConfig(enabled=True, allowed_users=["owner1"])
        channels["telegram"] = tg
    chat = FakeChannel("tg")
    channels["tg"] = chat
    conn = connect(cfg.db_path, embed_dim=FakeEmbedder.dim)
    providers = {n: FakeProvider(n, summary_json(title="摘要标题")) for n in ("claude:haiku", "claude:sonnet", "local")}
    llm = LLMRouter(providers, {"summarize": cfg.summarize, "answer": cfg.answer}, Settings(conn))
    web = WebStub()
    media = dataclasses.replace(fake_media(tmp_path), attachments_dir=cfg.attachments_dir)
    app = App(cfg, conn=conn, library=Library(cfg.library_dir, cfg.trash_dir, cfg.attachments_dir),
              index=Index(conn, FakeEmbedder()), llm=llm,
              registry=FetcherRegistry(fetchers if fetchers is not None else [web]), channels=channels, media=media)
    intake = Intake(app)
    channels["extension"].intake = intake
    app.web, app.tg, app.chat, app.providers = web, tg, chat, providers
    return app, intake


def body(**fields):
    return json.dumps(fields).encode()


def page(url=URL, html=ARTICLE_HTML, title="页面标题", **more):
    return parse_request(body(kind="page", url=url, title=title, html=html, **more))


async def items(intake):
    return (await intake.recent())["items"]


# ---- the request ----

@pytest.mark.parametrize("fields,reason", [
    ({"kind": "pdf", "url": URL}, BAD_KIND),
    ({"url": URL}, BAD_KIND),
    ({"kind": "page", "url": "chrome://extensions"}, NOT_A_WEB_PAGE),
    ({"kind": "page"}, NOT_A_WEB_PAGE),
    ({"kind": "page", "url": "http://[abc/def"}, NOT_A_WEB_PAGE),  # integration P17 x P19: urllib cannot parse it
    ({"kind": "page", "url": "http://:80/x"}, NOT_A_WEB_PAGE),
    ({"kind": "selection", "url": URL, "text": "  "}, NO_SELECTION),
    ({"kind": "image", "url": URL}, NO_IMAGE),
    ({"kind": "image", "url": URL, "image_url": "file:///etc/passwd"}, NO_IMAGE),
    ({"kind": "image", "url": URL, "image": {"data": "!!!", "mime": "image/png"}}, BAD_IMAGE),
    ({"kind": "image", "url": URL, "image": {"data": "aGk=", "mime": "text/html"}}, BAD_IMAGE),
])
def test_bad_requests_say_why_in_chinese(fields, reason):
    with pytest.raises(ValueError, match=reason):
        parse_request(body(**fields))


def test_not_json_is_refused():
    for raw in (b"{", b"\xff", b"[1]"):
        with pytest.raises(ValueError):
            parse_request(raw)


def test_a_request_is_normalized():
    req = parse_request(body(kind="selection", url="https://a.com/x?utm_source=t", title=" 标题\n  二 " * 100,
                             text="  选中的字  ", telegram="yes"))
    assert req.url == URL and req.text == "选中的字" and req.telegram is False  # only a real true
    assert req.title.startswith("标题 二 标题") and len(req.title) == 200
    assert parse_request(body(kind="selection", url="file:///x.html", text="字")).url is None
    img = parse_request(body(kind="image", url=URL, image={"data": base64.b64encode(PNG).decode(), "mime": "image/png"},
                             telegram=True))
    assert (img.image, img.image_mime, img.telegram) == (PNG, "image/png", True)


def test_the_tab_url_keeps_its_last_character():
    # P21 fix round 1 (review M8): the chat trim of trailing punctuation is for links typed in a sentence; the
    # extension sends the tab's exact link. Tracking parameters still go.
    dc = "https://en.wikipedia.org/wiki/Washington,_D.C."
    assert parse_request(body(kind="page", url=dc)).url == dc
    assert parse_request(body(kind="page", url=dc + "?utm_source=x")).url == dc
    assert parse_request(body(kind="selection", url=dc, text="字")).url == dc


def test_short_reason():
    assert short_reason("MediaError('附件文件不见了')") == "附件文件不见了"
    assert short_reason("网站拒绝访问（HTTP 403）") == "网站拒绝访问（HTTP 403）"
    assert short_reason("x" * 100) == "x" * 60 + "…"
    assert short_reason(None) == ""


# ---- page ----

async def test_a_page_is_saved_from_its_html_as_a_job(tmp_path):
    app, intake = build(tmp_path)
    rid = await intake.save(page())
    [item] = (await items(intake))
    assert (item["id"], item["kind"], item["title"], item["state"]) == (rid, "page", "页面标题", PENDING)
    staged = list(app.cfg.incoming_dir.glob("ext-*/page.html"))
    assert len(staged) == 1
    await app.process_due_jobs()
    [item] = (await items(intake))
    entry = app.library.get(item["entry_id"])
    assert (item["state"], item["entry_title"], item["summary"]) == (DONE, "页面标题", "这是摘要。")
    assert (entry.url, entry.title, entry.source) == (URL, "页面标题", "web")
    assert app.web.calls == []  # read from the page, never fetched
    assert not staged[0].exists() and not staged[0].parent.exists()  # the page is removed once saved
    assert app.tg.sent == [] and app.chat.sent == []  # no chat message by default


async def test_a_short_page_is_saved_as_its_link(tmp_path):
    app, intake = build(tmp_path)
    await intake.save(page(html=SHORT_HTML))
    await app.process_due_jobs()
    assert app.web.calls == [URL]
    [item] = (await items(intake))
    assert item["state"] == DONE and item["entry_title"] == "抓到的标题"


async def test_a_media_page_is_saved_as_its_link_without_staging_the_html(tmp_path):
    video = StubFetcher(FetchResult(url="https://www.youtube.com/watch?v=x", source="youtube", title="视频",
                                    text="字幕" * 200), handles="youtube.com")
    video.name = "youtube"
    app, intake = build(tmp_path, [video, WebStub()])
    await intake.save(page(url="https://www.youtube.com/watch?v=x"))
    assert not list(app.cfg.incoming_dir.glob("ext-*"))
    job = app.jobs.claim_due(1)[0]
    assert job.kind == "url" and job.payload == {"url": "https://www.youtube.com/watch?v=x"}  # as if sent in chat


async def test_a_page_without_html_is_a_link_save(tmp_path):
    app, intake = build(tmp_path)
    await intake.save(page(html=None))
    await app.process_due_jobs()
    assert app.web.calls == [URL] and (await items(intake))[0]["state"] == DONE


async def test_an_already_saved_link_is_done_at_once(tmp_path):
    app, intake = build(tmp_path)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    await app.process_due_jobs()
    saved = app.index.find_by_url(URL)
    await intake.save(page())
    [item] = (await items(intake))
    assert (item["state"], item["entry_id"]) == (DONE, saved)
    assert app.jobs.claim_due(5) == [] and not list(app.cfg.incoming_dir.glob("ext-*"))
    assert app.tg.sent == []


async def test_a_link_saved_before_p19_without_its_paren_is_a_duplicate_from_the_extension_too(tmp_path):
    # integration P17 x P19: the extension's queue_url gets the chat path's old-no-")" lookup
    wiki = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    app, intake = build(tmp_path)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    await app.process_due_jobs()
    saved = app.index.find_by_url(URL)
    app.conn.execute("UPDATE entries SET url = ?", (wiki[:-1],))
    await intake.save(page(url=wiki))
    [item] = (await items(intake))
    assert (item["state"], item["entry_id"]) == (DONE, saved)
    assert app.jobs.claim_due(5) == [] and not list(app.cfg.incoming_dir.glob("ext-*"))


async def test_a_renamed_note_is_a_duplicate_from_the_extension_too(tmp_path):
    # P21 fix round 1 (I1): renamed in Obsidian since the last scan, not a stale index row
    app, intake = build(tmp_path)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    await app.process_due_jobs()
    saved = app.index.find_by_url(URL)
    path = app.library.find(saved)
    path.rename(path.with_name("改了名字.md"))
    calls = app.index.embedder.calls
    await intake.save(page())
    [item] = (await items(intake))
    assert (item["state"], item["entry_id"]) == (DONE, saved)
    assert app.index.path_of(saved) is not None and app.jobs.claim_due(5) == []
    assert app.index.embedder.calls == calls


async def test_the_list_finds_a_note_renamed_since_the_last_scan(tmp_path):
    # P21 fix round 1 (review M1): not shown as deleted until the scan catches up
    app, intake = build(tmp_path)
    await intake.save(page())
    await app.process_due_jobs()
    [item] = await items(intake)
    path = app.library.find(item["entry_id"])
    path.rename(path.with_name("改了名字.md"))
    [item] = await items(intake)
    assert item["state"] == DONE and item["entry_title"] and item["summary"] != ENTRY_GONE


async def test_a_failed_link_is_filled_in_from_the_page(tmp_path):
    app, intake = build(tmp_path)
    app.web.result = FetchResult(url=URL, source="web", status="needs_manual", note="要登录")
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    await app.process_due_jobs()
    first = app.index.find_by_url(URL)
    assert app.library.get(first).status == "needs_manual"
    await intake.save(page())
    await app.process_due_jobs()
    [item] = (await items(intake))
    assert (item["state"], item["entry_id"], item["entry_title"]) == (DONE, first, "页面标题")
    assert app.web.calls == [URL]  # the chat link's own fetch only


async def test_a_link_being_saved_is_pending_then_done(tmp_path):
    app, intake = build(tmp_path)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    await intake.save(page())
    assert (await items(intake))[0]["state"] == PENDING
    await app.process_due_jobs()
    assert (await items(intake))[0]["state"] == DONE


async def test_a_page_nothing_could_be_read_from_is_failed(tmp_path):
    app, intake = build(tmp_path)
    app.web.result = FetchResult(url=URL, source="web", status="needs_manual", note="要登录")
    await intake.save(page(html=EMPTY_HTML))
    await app.process_due_jobs()
    [item] = (await items(intake))
    assert (item["state"], item["reason"]) == (FAILED, NOTHING_READ) and item["entry_id"]


# ---- selection ----

async def test_a_selection_is_a_note_from_its_page_never_a_question(tmp_path):
    app, intake = build(tmp_path)
    text = "这一段是从网页里选中的文字，它不是一个问题吗？"
    await intake.save(parse_request(body(kind="selection", url=URL, title="页面标题", text=text)))
    await app.process_due_jobs()
    [item] = (await items(intake))
    entry = app.library.get(item["entry_id"])
    assert (item["state"], entry.url, entry.title, entry.body) == (DONE, URL, "页面标题", text)
    assert app.providers["claude:sonnet"].calls == []  # never answered
    assert app.conn.execute("SELECT count(*) FROM actions").fetchone()[0] == 0  # no 改成提问
    # the page itself can still be saved: the note is not that page
    await intake.save(page())
    await app.process_due_jobs()
    assert (await items(intake))[0]["entry_id"] != entry.id


# ---- image ----

async def test_an_image_goes_through_ocr_with_its_page_link(tmp_path):
    app, intake = build(tmp_path)
    data = base64.b64encode(PNG).decode()
    await intake.save(parse_request(body(kind="image", url=URL, title="页面标题",
                                         image={"data": data, "mime": "image/png"})))
    await app.process_due_jobs()
    [item] = (await items(intake))
    entry = app.library.get(item["entry_id"])
    assert item["state"] == DONE and entry.url == URL and "图片文字" in entry.body
    files = list((app.media.attachments_dir / entry.id).iterdir())
    assert [f.read_bytes() for f in files] == [PNG] and files[0].suffix == ".png"


async def test_an_image_lands_in_the_vault_with_an_obsidian_link(tmp_path):
    # integration P17 x P18: with library_dir set, the image goes to <library>/attachments, staging stays in data_dir
    vault = tmp_path / "vault" / "Octopus"
    (tmp_path / "data").mkdir()
    app, intake = build(tmp_path / "data", library_folder=vault)
    data = base64.b64encode(PNG).decode()
    await intake.save(parse_request(body(kind="image", url=URL, title="页面标题",
                                         image={"data": data, "mime": "image/png"})))
    assert app.cfg.incoming_dir.is_relative_to(tmp_path / "data")
    await app.process_due_jobs()
    [item] = (await items(intake))
    entry = app.library.get(item["entry_id"])
    [file] = list((vault / "attachments" / entry.id).iterdir())
    assert file.read_bytes() == PNG and f"![[attachments/{entry.id}/{file.name}]]" in app.library.find(
        entry.id).read_text(encoding="utf-8")
    assert app.library.find(entry.id).is_relative_to(vault)
    assert not list(app.cfg.incoming_dir.glob("ext-*")) and not (tmp_path / "data" / "attachments" / entry.id).exists()


async def test_an_image_by_its_link_is_downloaded(tmp_path, monkeypatch):
    app, intake = build(tmp_path)
    seen = []

    def get(url, **kw):
        seen.append((url, kw["max_bytes"]))
        return httpx.Response(200, content=PNG, headers={"content-type": "image/jpeg"})

    monkeypatch.setattr("octopus.extension.http.get", get)
    await intake.save(parse_request(body(kind="image", url=URL, image_url="https://img.a.com/p/cat.jpg?x=1")))
    await app.process_due_jobs()
    assert seen == [("https://img.a.com/p/cat.jpg?x=1", 15 * 1024 * 1024)]
    [item] = (await items(intake))
    files = list((app.media.attachments_dir / item["entry_id"]).iterdir())
    assert [f.name for f in files] == ["cat.jpg"]


async def test_an_image_that_cannot_be_downloaded_fails_at_once(tmp_path, monkeypatch):
    from octopus.fetchers.base import FetchError

    app, intake = build(tmp_path)

    def get(url, **kw):
        raise FetchError("网站拒绝访问（HTTP 403）", retryable=False)

    monkeypatch.setattr("octopus.extension.http.get", get)
    await intake.save(parse_request(body(kind="image", url=URL, image_url="https://img.a.com/cat.jpg")))
    [item] = (await items(intake))
    assert (item["state"], item["reason"]) == (FAILED, "图片下载失败：网站拒绝访问（HTTP 403）")
    assert app.jobs.claim_due(5) == []
    monkeypatch.setattr("octopus.extension.http.get",
                        lambda url, **kw: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}))
    await intake.save(parse_request(body(kind="image", url=URL, image_url="https://img.a.com/cat.jpg")))
    assert (await items(intake))[0]["reason"] == "图片下载失败：这个链接不是图片"


async def test_a_failed_job_shows_its_reason(tmp_path):
    app, intake = build(tmp_path)
    data = base64.b64encode(PNG).decode()
    await intake.save(parse_request(body(kind="image", url=URL, image={"data": data, "mime": "image/png"})))
    for f in app.cfg.incoming_dir.glob("ext-*/*"):
        f.unlink()  # the staged image is gone: the save cannot work
    await app.process_due_jobs()
    [item] = (await items(intake))
    assert (item["state"], item["reason"]) == (FAILED, "保存失败：附件文件不见了")


# ---- Telegram ----

async def test_with_telegram_on_the_saved_message_goes_to_the_owner(tmp_path):
    app, intake = build(tmp_path)
    await intake.save(page(telegram=True))
    await app.process_due_jobs()
    [(owner, text)] = app.tg.sent
    assert owner == "owner1" and text.startswith("✅ 已保存：页面标题")
    await intake.save(page(telegram=True))  # saved already: what the chat would say
    assert app.tg.sent[-1][1].startswith("这个链接已经存过了：页面标题")
    await intake.save(page(url="https://b.com/y", telegram=False))
    await app.process_due_jobs()
    assert len(app.tg.sent) == 2


async def test_with_telegram_not_configured_the_flag_is_ignored_and_logged_once(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    app, intake = build(tmp_path, telegram=False)
    assert (await intake.recent())["telegram"] is False
    await intake.save(page(telegram=True))
    await intake.save(page(url="https://b.com/y", telegram=True))
    await app.process_due_jobs()
    assert [i["state"] for i in (await items(intake))] == [DONE, DONE]
    told = [r for r in caplog.records if "Telegram is not running" in r.getMessage()]
    assert len(told) == 1


async def test_telegram_is_offered_only_when_it_runs_with_an_owner(tmp_path):
    app, intake = build(tmp_path)
    assert (await intake.recent())["telegram"] is True
    app.cfg.channels["telegram"].allowed_users = []
    assert (await intake.recent())["telegram"] is False


# ---- restart, list, log ----

async def test_the_list_survives_a_restart(tmp_path):
    app, intake = build(tmp_path)
    await intake.save(page())
    await intake.save(parse_request(body(kind="selection", url=URL, text="选中的字")))
    await app.process_due_jobs()
    await intake.save(page(url="https://b.com/y"))  # still queued when the service stops
    before = (await items(intake))
    app.close()
    again, intake2 = build(tmp_path)
    assert (await items(intake2)) == before
    assert [i["state"] for i in before] == [PENDING, DONE, DONE]
    await again.process_due_jobs()
    assert (await items(intake2))[0]["state"] == DONE


async def test_the_list_is_the_latest_ten_newest_first(tmp_path):
    app, intake = build(tmp_path)
    ids = [await intake.save(parse_request(body(kind="selection", url=URL, text=f"第{i}段"))) for i in range(12)]
    assert [i["id"] for i in (await items(intake))] == ids[::-1][:10]


async def test_each_request_gets_one_in_line_with_the_host_only(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    app, intake = build(tmp_path)
    rid = await intake.save(page(url="https://a.com/private/path?token=secret", telegram=True))
    await intake.save(parse_request(body(kind="selection", url=URL, text="选中的文字" * 10)))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("in extension")]
    tag = user_tag(f"ext-{rid}")
    assert lines[0] == f'in extension {tag} #{rid} page {len(ARTICLE_HTML)}字 tg -> page "链接 a.com"'
    assert lines[1].endswith('selection 50字 -> note "选中的文字选中的文字选中的文字选中的文字…" "链接 a.com"')
    assert "private" not in caplog.text and "secret" not in caplog.text
    await app.process_due_jobs()
    out = [r.getMessage() for r in caplog.records if r.getMessage().startswith(f"out extension {tag}")]
    assert len(out) == 1 and out[0].endswith(" ok")  # the outcome, as for a chat save


async def test_the_page_stays_while_the_job_will_run_again(tmp_path):
    from octopus.fetchers.base import FetchError

    app, intake = build(tmp_path)
    app.web.result = FetchError("网络错误", retryable=True)
    await intake.save(page(html=SHORT_HTML))
    [staged] = app.cfg.incoming_dir.glob("ext-*/page.html")
    await app.process_due_jobs()
    assert app.jobs.counts() == {"queued": 1} and staged.exists()  # the retry reads it again
    assert (await items(intake))[0]["state"] == PENDING
    job = app.jobs.get(1)
    app.conn.execute("UPDATE jobs SET status = 'running' WHERE id = 1")
    app.jobs.retry_or_fail(1, "网站拒绝访问（HTTP 403）", retry=False)
    app._drop_page(job)
    assert not staged.exists() and (await items(intake))[0]["state"] == FAILED


# ---- fix round 1 ----

async def test_a_stale_index_row_makes_a_link_job_not_a_refetch(tmp_path):
    app, intake = build(tmp_path)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    await app.process_due_jobs()
    app.library.find(app.index.find_by_url(URL)).unlink()  # the Markdown deleted by hand
    what, job, entry = await app.queue_url(URL, "extension", "ext-9")
    assert (what, entry) == ("queued", None) and app.jobs.get(job).kind == "url"
    assert app.jobs.get(job).payload == {"url": URL}


async def test_a_slow_image_download_is_given_up(tmp_path, monkeypatch):
    import time

    app, intake = build(tmp_path)
    monkeypatch.setattr("octopus.extension.IMAGE_DOWNLOAD_SECONDS", 0.1)

    def slow(url, **kw):
        time.sleep(0.5)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    monkeypatch.setattr("octopus.extension.http.get", slow)
    started = time.monotonic()
    await intake.save(parse_request(body(kind="image", url=URL, image_url="https://img.a.com/cat.png")))
    assert time.monotonic() - started < 0.4
    assert (await items(intake))[0]["reason"] == "图片下载失败：太慢了，没下载完"
    from octopus.extension import IMAGE_DOWNLOAD_SECONDS

    assert IMAGE_DOWNLOAD_SECONDS == 0.1


def test_the_real_image_download_limit_is_15_seconds():
    import octopus.extension as ext

    assert ext.IMAGE_DOWNLOAD_SECONDS == 15


async def test_telegram_copies_never_ask_for_screenshots_to_merge(tmp_path):
    app, intake = build(tmp_path)
    app.web.result = FetchResult(url=URL, source="web", status="needs_manual", note="要登录")
    await intake.save(page(html=EMPTY_HTML, telegram=True))
    await app.process_due_jobs()
    [(_, text)] = app.tg.sent
    eid = (await items(intake))[0]["entry_id"]
    assert "合并到这一条" not in text and f"要补充就在聊天里发 /add {eid}，再发截图或原文。" in text
    app.tg.sent.clear()
    await intake.save(page(url="https://b.com/y", html=SHORT_HTML, telegram=True))  # partial: short page kept
    app.web.result = FetchResult(url="https://b.com/y", source="web", status="needs_manual", note="要登录")
    await app.process_due_jobs()
    [(_, text)] = app.tg.sent
    assert "可以补发截图或录屏" not in text and "要补充就在聊天里发 /add " in text


async def test_an_image_entry_opens_no_supplement_window(tmp_path):
    app, intake = build(tmp_path)
    app.media = app.pipeline.media = fake_media(tmp_path, fail="ocr")
    data = base64.b64encode(PNG).decode()
    rid = await intake.save(parse_request(body(kind="image", url=URL, image={"data": data, "mime": "image/png"})))
    await app.process_due_jobs()
    entry = app.library.get((await items(intake))[0]["entry_id"])
    assert entry.status == "partial" and entry.url == URL
    assert app.pending.active("extension", f"ext-{rid}") is None


async def test_old_requests_leave_no_rows_behind(tmp_path, monkeypatch):
    monkeypatch.setattr("octopus.extension.KEEP_ROWS", 2)
    app, intake = build(tmp_path)
    for i in range(4):
        await intake.save(parse_request(body(kind="selection", url=URL, text=f"第{i}段")))
        await app.process_due_jobs()
    users = {r[0] for r in app.conn.execute("SELECT user_id FROM focus WHERE channel = 'extension'")}
    assert users <= {"ext-3", "ext-4"} and [i["id"] for i in (await items(intake))] == [4, 3]



# ---- fix round 2 ----

async def test_an_incomplete_entry_says_so_in_the_list_and_on_telegram(tmp_path):
    from test_pipeline_page import SHORT_POST_HTML

    from octopus.extension import INCOMPLETE

    app, intake = build(tmp_path)
    app.web.result = FetchResult(url=URL, source="web", status="needs_manual", note="要登录")
    await intake.save(page(html=SHORT_POST_HTML, telegram=True))
    await app.process_due_jobs()
    [item] = await items(intake)
    assert (item["state"], item["incomplete"], item["reason"]) == (DONE, True, INCOMPLETE)
    [(_, text)] = app.tg.sent
    assert text.startswith("⚠️ 已保存（内容不完整）：页面标题")
    await intake.save(page(url="https://b.com/y"))
    await app.process_due_jobs()
    assert (await items(intake))[0]["incomplete"] is False


async def test_the_list_reads_the_database_on_the_loop_and_files_in_a_thread(tmp_path, monkeypatch):
    import threading

    app, intake = build(tmp_path)
    await intake.save(page())
    await app.process_due_jobs()
    seen = {}
    real_latest, real_load, real_stored = intake.saves.latest, app.library.load, app.library.index_path
    monkeypatch.setattr(intake.saves, "latest",
                        lambda n: seen.setdefault("db", threading.current_thread()) and real_latest(n))
    monkeypatch.setattr(app.library, "load",
                        lambda path: seen.setdefault("file", threading.current_thread()) and real_load(path))
    # integration P17 x P18: finding an entry's file asks the index (the database) when the id is not known yet
    monkeypatch.setattr(app.library, "index_path",
                        lambda eid: seen.setdefault("index", threading.current_thread()) and real_stored(eid))
    app.library._ids.clear()
    assert (await items(intake))[0]["state"] == DONE
    assert seen["db"] is threading.main_thread() and seen["file"] is not threading.main_thread()
    assert seen["index"] is threading.main_thread()



async def test_a_short_x_post_is_kept_when_syndication_is_unreachable(tmp_path):
    from test_pipeline_page import SHORT_POST, SHORT_POST_HTML

    from octopus.fetchers.base import FetchError

    syndication = StubFetcher(FetchError("网络错误：ConnectError", retryable=True), handles="x.com/")
    syndication.name = "x"
    app, intake = build(tmp_path, [syndication, WebStub()])
    await intake.save(page(url="https://x.com/a/status/1", html=SHORT_POST_HTML, title="X 上的帖子"))
    await app.process_due_jobs()
    assert app.jobs.counts() == {}  # done at once: no retry waiting, never needs_manual
    [item] = await items(intake)
    entry = app.library.get(item["entry_id"])
    assert (item["state"], item["incomplete"]) == (DONE, True)
    assert (entry.status, entry.source, entry.body) == ("partial", "x", SHORT_POST)


# ---- P25 (review Q-b): a language-free code next to the state the old popup matches ----

async def test_each_item_has_a_code_beside_its_state(tmp_path):
    app, intake = build(tmp_path)
    await intake.save(page())
    [item] = await items(intake)
    assert (item["state"], item["code"]) == (PENDING, "processing")
    await app.process_due_jobs()
    [item] = await items(intake)
    assert (item["state"], item["code"]) == (DONE, "saved")
    data = base64.b64encode(PNG).decode()
    await intake.save(parse_request(body(kind="image", url=URL, image={"data": data, "mime": "image/png"})))
    for f in app.cfg.incoming_dir.glob("ext-*/*"):
        f.unlink()
    await app.process_due_jobs()
    assert [(i["state"], i["code"]) for i in await items(intake)][0] == (FAILED, "failed")


def test_an_incomplete_save_has_its_own_code():
    from octopus.extension import code_of

    assert code_of({"state": DONE, "incomplete": True}) == "incomplete"
    assert [code_of({"state": s}) for s in (PENDING, DONE, FAILED)] == ["processing", "saved", "failed"]
