import asyncio
import logging
import os
import time

import pytest

from fakes import FakeChannel, FakeEmbedder, FakeProvider, StubFetcher, fake_media, summary_json

from octopus.app import NEW_DONE, SAVE_CHAT, VOICE_UNCLEAR, App
from octopus.config import ChannelConfig, make_test_config
from octopus.db import connect
from octopus.fetchers import FetcherRegistry
from octopus.fetchers.base import FetchError, FetchResult
from octopus.index import Index
from octopus.library import Library
from octopus.llm.router import LLMRouter
from octopus.models import Attachment, IncomingMessage
from octopus.settings import Settings


def make_app(tmp_path, fetcher=None):
    cfg = make_test_config(tmp_path)
    cfg.channels["tg"] = ChannelConfig(enabled=True, allowed_users=["me"])
    conn = connect(cfg.db_path, embed_dim=FakeEmbedder.dim)
    providers = {n: FakeProvider(n, summary_json(title="标题")) for n in ("claude:haiku", "claude:sonnet", "local")}
    llm = LLMRouter(providers, {"summarize": cfg.summarize, "answer": cfg.answer}, Settings(conn))
    ch = FakeChannel("tg")
    media = fake_media(tmp_path)
    app = App(cfg, conn=conn, library=Library(cfg.library_dir, cfg.trash_dir), index=Index(conn, FakeEmbedder()),
              llm=llm, registry=FetcherRegistry([fetcher] if fetcher else []), channels={ch.name: ch}, media=media)
    return app, ch, media


def staged(tmp_path, name, kind="image"):
    d = tmp_path / "attachments" / "_incoming" / "x"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"x")
    return Attachment(kind=kind, path=d / name, filename=name)


def m(text="", attachments=None):
    return IncomingMessage(channel="tg", user_id="me", text=text, attachments=attachments or [])


async def test_screenshot_saved_as_entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    assert ch.sent[-1][1] == "收到，处理中…"
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存：标题")
    assert app.index.count() == 1


async def test_screenshot_after_needs_manual_is_merged(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("⚠️ 没能获取内容")
    await app.handle(m("", [staged(tmp_path, "shot.png")]))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到：标题")
    assert app.index.count() == 1
    assert app.pending.active("tg", "me") is not None  # window restarts after a merge


async def test_note_within_window_is_merged_but_question_is_not(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    await app.handle(m("这个帖子讲了复利"))  # short plain text in the window: chat, not a supplement
    assert app.jobs.claim_due(10) == []
    assert [b.label for b in ch.sent_full[-1][1]] == [SAVE_CHAT]
    await app.handle(m("这是我从帖子里复制的正文\n第二段"))  # pasted (multi-line): a supplement
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到")
    assert app.index.count() == 1
    await app.handle(m("/s 我自己的一条笔记"))  # /s is always a new entry, the window stays on the link
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存") and app.index.count() == 2


async def test_reply_to_prompt_merges(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    app.pending.set("tg", "me", eid, prompt_message_id="bot-msg-1")
    later = time.time() + 2 * 3600
    app.pending.clock = lambda: later  # outside the 30-min merge window, inside the 7-day prompt TTL
    msg = m("", [staged(tmp_path, "late.png")])
    msg.reply_to_id = "bot-msg-1"
    await app.handle(msg)
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到")


async def test_resend_of_needs_manual_url_refetches_same_entry(tmp_path):
    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="网络错误"))
    app, ch, _ = make_app(tmp_path, fetcher)
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    first_id = app.index.find_by_url("https://a.com/x")
    fetcher.result = FetchResult(url="https://a.com/x", source="web", title="好了", text="正文" * 200)
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存：好了")
    assert app.index.find_by_url("https://a.com/x") == first_id
    assert app.index.count() == 1


async def test_resend_while_refetch_queued_says_in_progress(tmp_path):
    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="网络错误"))
    app, ch, _ = make_app(tmp_path, fetcher)
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    await app.handle(m("https://a.com/x"))  # queues a refetch
    await app.handle(m("https://a.com/x"))
    assert ch.sent[-1][1] == "这个链接正在处理中，请稍等。"
    assert app.jobs.counts() == {"queued": 1}


async def test_refetch_of_entry_supplemented_meanwhile_replies_duplicate(tmp_path):
    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="网络错误"))
    app, ch, _ = make_app(tmp_path, fetcher)
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    await app.handle(m("https://a.com/x"))  # refetch queued
    await app.handle(m("我手动粘贴的正文\n第二段"))
    refetch_job, merge_job = app.jobs.claim_due(10)
    assert (refetch_job.kind, merge_job.kind) == ("refetch", "merge")
    await app._run_job(merge_job)  # the merge lands before the refetch runs
    await app._run_job(refetch_job)
    sent = [t for _, t in ch.sent]
    assert any(t.startswith("✅ 已补充到") for t in sent)
    assert sent[-1].startswith("这个链接已经存过了")
    assert app.index.count() == 1


async def test_failed_refetch_does_not_create_a_second_entry(tmp_path):
    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="网络错误"))
    app, ch, _ = make_app(tmp_path, fetcher)
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    fetcher.result = FetchError("网站拒绝访问（HTTP 403）", retryable=False)
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("⚠️ 没能获取内容")
    assert app.index.count() == 1


async def test_attachment_job_reserves_entry_id_and_retry_is_idempotent(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    job = app.jobs.claim_due(1)[0]
    eid = job.payload["entry_id"]
    assert eid
    out1 = await app._execute(job)
    out2 = await app._execute(job)  # a retry after the first attempt already saved
    assert out1.entry.id == out2.entry.id == eid
    assert app.index.count() == 1


async def test_merge_job_carries_fallback_id_and_falls_back_when_entry_deleted(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    await app.handle(m("", [staged(tmp_path, "shot.png")]))
    job = app.jobs.claim_due(1)[0]
    assert job.kind == "merge" and job.payload["fallback_id"]
    await app.pipeline.delete(eid)
    out = await app._execute(job)
    assert out.entry.id == job.payload["fallback_id"] != eid


async def test_refetch_of_deleted_entry_returns_existing_entry_for_url(tmp_path):
    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="网络错误"))
    app, ch, _ = make_app(tmp_path, fetcher)
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    old_id = app.index.find_by_url("https://a.com/x")
    await app.handle(m("https://a.com/x"))  # queues a refetch of old_id
    job = app.jobs.claim_due(1)[0]
    assert job.kind == "refetch"
    await app.pipeline.delete(old_id)
    fetcher.result = FetchResult(url="https://a.com/x", source="web", title="好了", text="正文" * 200)
    new_id = (await app.pipeline.save_url("https://a.com/x", "tg")).entry.id  # saved again before the job runs
    assert await app._execute(job) == new_id
    assert app.index.count() == 1


async def test_partial_merge_keeps_pending_target(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    app.pipeline.media = fake_media(tmp_path, fail="ocr")
    await app.handle(m("", [staged(tmp_path, "bad.png")]))
    await app.process_due_jobs()
    assert app.library.get(eid).status == "partial"
    assert app.pending.active("tg", "me") == eid  # still the merge target
    app.pipeline.media = fake_media(tmp_path)
    await app.handle(m("", [staged(tmp_path, "good.png")]))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到")
    assert app.index.count() == 1
    assert app.pending.active("tg", "me") == eid  # still open after a complete merge


async def test_url_with_attachment_saves_both(tmp_path):
    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", title="文章", text="正文" * 200))
    app, ch, _ = make_app(tmp_path, fetcher)
    shot = staged(tmp_path, "a.png")
    await app.handle(m("https://a.com/x 看这张图", [shot]))
    jobs = app.jobs.claim_due(10)
    assert sorted(j.kind for j in jobs) == ["attachments", "url"]
    att = next(j for j in jobs if j.kind == "attachments")
    assert att.payload["caption"] == "https://a.com/x 看这张图" and att.payload["entry_id"]
    assert [t for _, t in ch.sent] == ["收到，处理中…"]  # one ack for the whole message
    await asyncio.gather(*(app._run_job(j) for j in jobs))
    assert app.index.count() == 2
    assert not shot.path.exists()  # moved into attachments/<id>/, not left in _incoming


async def test_url_with_attachment_is_its_own_entry_even_with_a_window(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    assert app.pending.active("tg", "me")
    await app.handle(m("https://a.com/y", [staged(tmp_path, "shot.png")]))
    jobs = app.jobs.claim_due(10)
    assert sorted(j.kind for j in jobs) == ["attachments", "url"]
    assert app.pending.active("tg", "me") is None


PASTED_1 = ("楼主说：我一直在想，复利到底是什么？很多人以为它只是利滚利，但其实更重要的是时间。\n"
            "如果你二十岁开始每月定投，到六十岁会有多少钱呢？答案可能会让你吃惊。")
PASTED_2 = ("评论区一位网友回复：你真的理解什么叫长期主义吗？长期主义不是买了以后就再也不管，而是在市场最恐慌、"
            "所有人都在割肉离场的时候还能按计划坚持定投，这需要非常强的心理素质，也需要对自己判断的信心，"
            "说实话大多数人都做不到这一点。")


async def test_pasted_text_with_question_words_is_merged_in_window(tmp_path):
    assert "\n" not in PASTED_2 and len(PASTED_2) >= 80
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    await app.handle(m(PASTED_1))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到")
    app.pending.set("tg", "me", eid)
    await app.handle(m(PASTED_2))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到")
    body = app.library.get(eid).body
    assert "复利到底是什么" in body and "长期主义" in body
    assert app.index.count() == 1


async def test_short_or_explicit_question_in_window_is_answered(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    for q in ("这个帖子讲了什么？", "问：" + PASTED_1):
        sent = len(ch.sent)
        await app.handle(m(q))
        assert app.jobs.claim_due(10) == []
        assert len(ch.sent) == sent + 1 and ch.sent[-1][1] != "收到，处理中…"  # an answer, not a save ack
    assert app.pending.active("tg", "me") is not None


async def test_missing_attachment_fails_without_retry_and_tells_user(tmp_path):
    app, ch, _ = make_app(tmp_path)
    shot = staged(tmp_path, "a.png")
    await app.handle(m("", [shot]))
    shot.path.unlink()  # e.g. cleaned up before the job ran
    await app.process_due_jobs()
    assert app.jobs.counts() == {"failed": 1}
    assert "附件文件不见了" in ch.sent[-1][1]


async def test_partial_attachment_entry_asks_for_nothing(tmp_path):
    app, ch, _ = make_app(tmp_path)
    app.pipeline.media = fake_media(tmp_path, fail="ocr")
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    [eid] = [e.id for _, e in app.library.iter_entries()]
    assert app.library.get(eid).status == "partial"
    assert app.pending.origin("tg", "me") == "auto"  # P8: the images sent next join it, it asks for nothing
    await app.handle(m("/s 一条普通的笔记"))
    assert [j.kind for j in app.jobs.claim_due(10)] == ["note"]


async def test_worker_start_sweeps_leftover_downloads(tmp_path):
    app, ch, _ = make_app(tmp_path)
    dl = app.cfg.downloads_dir
    dl.mkdir(parents=True, exist_ok=True)
    for name in ("abc.m4a", "def.mp3.part", "ghi.pdf"):
        (dl / name).write_bytes(b"x")
    stop = asyncio.Event()
    stop.set()
    await app.run_worker(stop)
    assert list(dl.iterdir()) == []


async def test_worker_start_sweeps_staged_files_nobody_will_read(tmp_path):
    # P19: a stop mid-download, a message nobody handled, a job that failed for good leave staged files behind
    app, ch, _ = make_app(tmp_path)
    inc = app.cfg.incoming_dir
    old = time.time() - 25 * 3600

    def stage(name, age=old):
        d = inc / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.png").write_bytes(b"x")
        for p in (d / "a.png", d):
            os.utime(p, (age, age))
        return d / "a.png"

    orphan, fresh, needed = stage("orphan"), stage("fresh", time.time() - 3600), stage("needed")
    loose = inc / "loose.txt"
    loose.write_bytes(b"x")
    os.utime(loose, (old, old))
    app.jobs.enqueue("attachments", {"files": [{"kind": "image", "path": str(needed)}], "caption": "",
                                     "entry_id": "x"}, "tg", "me")  # not run yet (the service was off a day)
    stop = asyncio.Event()
    stop.set()
    await app.run_worker(stop)
    assert sorted(p.name for p in inc.iterdir()) == ["fresh", "needed"]
    assert fresh.exists() and needed.exists() and not orphan.exists()


async def test_worker_start_keeps_the_page_a_queued_extension_job_will_read(tmp_path):
    # integration P17 x P19: the Chrome extension's staged page is a job's "page", not one of its "files"
    app, ch, _ = make_app(tmp_path)
    d = app.cfg.incoming_dir / "ext-1"
    d.mkdir(parents=True)
    page = d / "page.html"
    page.write_text("<html></html>")
    old = time.time() - 25 * 3600
    for p in (page, d):
        os.utime(p, (old, old))
    app.jobs.enqueue("url", {"url": "https://a.com/x", "page": str(page), "title": "t"}, "extension", "me")
    app._sweep_incoming()
    assert page.exists()


async def test_incoming_sweep_never_touches_saved_attachments(tmp_path):
    # integration P18 x P19: in the default layout _incoming sits inside attachments/, next to the entries' files
    app, ch, _ = make_app(tmp_path)
    old = time.time() - 25 * 3600
    saved = app.cfg.attachments_dir / "20250101-abc123" / "a.png"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"x")
    app.cfg.incoming_dir.mkdir(parents=True)
    for p in (saved, saved.parent, app.cfg.incoming_dir, app.cfg.attachments_dir):
        os.utime(p, (old, old))
    app._sweep_incoming()
    assert saved.exists() and app.cfg.incoming_dir.is_dir()


async def test_incoming_sweep_counts_only_what_it_removed(tmp_path, monkeypatch, caplog):
    # P19 fix round 1 (M9): rmtree(ignore_errors=True) can fail quietly
    from octopus import app as app_module

    app, ch, _ = make_app(tmp_path)
    d = app.cfg.incoming_dir / "stuck"
    d.mkdir(parents=True)
    (d / "a.png").write_bytes(b"x")
    old = time.time() - 25 * 3600
    for p in (d / "a.png", d):
        os.utime(p, (old, old))
    monkeypatch.setattr(app_module.shutil, "rmtree", lambda path, ignore_errors=False: None)
    with caplog.at_level(logging.INFO):
        app._sweep_incoming()
    assert d.exists() and "removed" not in caplog.text


async def test_photo_captioned_cun_is_saved_as_attachment_entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("存", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存：标题")
    assert app.index.count() == 1


async def test_wechat_voice_transcript_is_saved_as_a_recording(tmp_path):
    # final review I3: ClawBot hands voice over as an audio attachment without a file
    app, ch, media = make_app(tmp_path)
    await app.handle(m("", [Attachment(kind="audio", path=None, filename="voice.silk", text="明天下午三点开会")]))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存")
    assert app.index.count() == 1 and media.calls == []


async def test_reply_to_an_older_prompt_merges_into_that_entry(tmp_path):
    # final review I4: link X, then link Y (both need screenshots); a reply to X's prompt goes to X
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    x_prompt, x = f"m{len(ch.sent)}", app.pending.active("tg", "me")
    await app.handle(m("https://www.reddit.com/r/x/comments/2"))
    await app.process_due_jobs()
    y = app.pending.active("tg", "me")
    assert x != y and ch.sent[-1][1].startswith("⚠️ 没能获取内容")
    msg = m("", [staged(tmp_path, "for-x.png")])
    msg.reply_to_id = x_prompt
    await app.handle(msg)
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到")
    assert "图片文字" in app.library.get(x).body
    assert app.library.get(y).status == "needs_manual" and "图片文字" not in app.library.get(y).body


# ---- P5: hold-to-talk voice is handled as the words said (spec §2.2 rule 8) ----

def voice(tmp_path, name="voice.ogg"):
    a = staged(tmp_path, name, kind="audio")
    a.voice = True
    return a


async def test_wechat_voice_with_save_word_queues_a_note(tmp_path):
    app, ch, media = make_app(tmp_path)
    await app.handle(m("", [Attachment(kind="audio", filename="voice.silk", voice=True, text="存，明天开会")]))
    [job] = app.jobs.claim_due(10)
    assert (job.kind, job.payload) == ("note", {"text": "明天开会"})
    assert media.calls == []  # WeChat's own transcript: no Whisper


async def test_voice_question_is_answered_and_recording_discarded(tmp_path):
    app, ch, _ = make_app(tmp_path)
    app.media = fake_media(tmp_path, transcript="什么是复利")
    v = voice(tmp_path)
    await app.handle(m("", [v]))
    assert not v.path.exists()
    assert app.jobs.claim_due(10) == []
    text, buttons = ch.sent_full[-1]
    assert "这是摘要" in text and [b.label for b in buttons] == [SAVE_CHAT]  # the fake model answered with no hits
    assert app.convos.recent("tg", "me")[0] == ("user", "什么是复利")
    assert app.media.calls == [("asr", str(v.path))]


async def test_voice_transcript_becomes_a_typed_command(tmp_path):
    app, ch, _ = make_app(tmp_path)
    app.media = fake_media(tmp_path, transcript="/s 明天给老王回电话")
    await app.handle(m("", [voice(tmp_path)]))
    [job] = app.jobs.claim_due(10)
    assert job.payload == {"text": "明天给老王回电话"}


@pytest.mark.parametrize("media_kw", [{"transcript": ""}, {"transcript": "   "}, {"fail": "asr"}])
async def test_unclear_voice_is_not_saved(tmp_path, media_kw):
    app, ch, _ = make_app(tmp_path)
    app.media = fake_media(tmp_path, **media_kw)
    v = voice(tmp_path)
    await app.handle(m("", [v]))
    assert ch.sent[-1][1] == VOICE_UNCLEAR == "没听清，请再说一次或者打字。"
    assert app.jobs.claim_due(10) == [] and app.index.count() == 0
    assert not v.path.exists() and not v.path.parent.exists()


async def test_voice_without_media_tools_is_unclear(tmp_path):
    app, ch, _ = make_app(tmp_path)
    app.media = None
    v = voice(tmp_path)
    await app.handle(m("", [v]))
    assert ch.sent[-1][1] == VOICE_UNCLEAR
    assert not v.path.exists()


async def _open_window(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    return app, ch, app.pending.active("tg", "me")


async def test_several_screenshots_one_after_another_land_in_one_entry(tmp_path):
    app, ch, eid = await _open_window(tmp_path)
    for i in range(4):
        await app.handle(m("", [staged(tmp_path, f"s{i}.png")]))
        await app.process_due_jobs()
        assert ch.sent[-1][1].startswith("✅ 已补充到")
    assert app.index.count() == 1 and app.pending.active("tg", "me") == eid


async def test_link_clears_the_window_so_next_image_is_new(tmp_path):
    app, ch, eid = await _open_window(tmp_path)
    await app.handle(m("https://a.com/y", [staged(tmp_path, "with-link.png")]))
    assert sorted(j.kind for j in app.jobs.claim_due(10)) == ["attachments", "url"]  # no merge job
    await app.handle(m("", [staged(tmp_path, "later.png")]))
    assert [j.kind for j in app.jobs.claim_due(10)] == ["attachments"]


@pytest.mark.parametrize("text", ["/new", "新的", "新的一条", "单独存下一条。", "/NEW"])
async def test_new_ends_the_window(tmp_path, text):
    app, ch, eid = await _open_window(tmp_path)
    await app.handle(m(text))
    assert ch.sent[-1][1] == NEW_DONE
    assert app.pending.active("tg", "me") is None and app.pending.fresh("tg", "me")
    await app.handle(m("", [staged(tmp_path, "x.png")]))
    assert [j.kind for j in app.jobs.claim_due(10)] == ["attachments"]


async def test_new_without_a_window_replies_the_same(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    assert ch.sent[-1][1] == NEW_DONE == ("好的，接下来发的图片、文件，还有第一段文字，会存成新的一条；"
                                          "发“好了”、命令，或 5 分钟不发就结束。")


async def test_window_expires_1800s_after_the_last_merge(tmp_path):
    app, ch, eid = await _open_window(tmp_path)
    now = [time.time()]
    app.pending.clock = lambda: now[0]
    now[0] += 1700
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()  # merge at t+1700 restarts the window
    now[0] += 1700  # 3400 s after the link, 1700 after the merge
    assert app.pending.active("tg", "me") == eid
    now[0] += 101
    assert app.pending.active("tg", "me") is None


async def test_resending_the_windows_own_link_keeps_it_other_links_clear_it(tmp_path):
    app, ch, eid = await _open_window(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))  # refetch of the window's entry
    assert [j.kind for j in app.jobs.claim_due(10)] == ["refetch"]
    assert app.pending.active("tg", "me") == eid
    await app.handle(m("https://a.com/other"))
    assert app.pending.active("tg", "me") is None


async def test_merge_finishing_after_new_does_not_reopen_the_window(tmp_path):
    app, ch, eid = await _open_window(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))  # merge job queued, not run yet
    await app.handle(m("/new"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到")
    assert app.pending.active("tg", "me") is None
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    assert [j.kind for j in app.jobs.claim_due(10)] == ["attachments"]


async def test_merge_into_deleted_entry_moves_the_window_to_the_new_entry(tmp_path):
    app, ch, eid = await _open_window(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.pipeline.delete(eid)  # deleted after the merge was queued
    await app.process_due_jobs()
    new = app.pending.active("tg", "me")
    assert new and new != eid and app.library.get(new)
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    [job] = app.jobs.claim_due(10)
    assert job.kind == "merge" and job.payload["entry_id"] == new


def test_help_block_for_supplements():
    from octopus.commands import HELP
    assert """【补充到已有记录】
· 连着发的几张图片自动存成一条（一篇文章的截图直接发就行），发文字就算发完了
· 链接读不出来时，接下来 30 分钟内发的截图、粘贴的原文会加到那一条
· /add 记录号 → 接下来发的图片、文件和第一段文字都加到这条
· /add 记录号 文字 → 马上把这段文字加到这条
· /new（或“新的”）→ 马上开始新的一条，接下来发的图片、文件和第一段文字都存进去
· /add、/new 之后：发第二段文字、命令、链接或“好了”（/done），或者 5 分钟没再发，就算发完了；/s 存的笔记总是单独一条
· 加错了：点【不是这条，单独存】（微信回“单独存”）
· 记录号可以打全，也可以只打“-”后面的 6 位，或其中前 4 位、后 4 位，例：/delete 6e43""" in HELP
    assert ("/add 记录号 — 往这条里补充图片或文字\n/new — 马上开始新的一条，接下来发的图片、文件和第一段文字都存进去\n"
            "/done — 结束 /add、/new 的补充（也可以说“好了”）") in HELP
    assert "· /s 内容（也可以 /save、“存 内容”、“存：内容”）→ 把文字存成新的一条笔记" in HELP
