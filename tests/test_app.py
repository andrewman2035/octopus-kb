from fakes import FakeChannel, FakeEmbedder, FakeProvider, StubFetcher, summary_json

from octopus.app import App
from octopus.config import ChannelConfig, make_test_config
from octopus.db import connect
from octopus.fetchers import FetcherRegistry
from octopus.fetchers.base import FetchError, FetchResult
from octopus.index import Index
from octopus.library import Library
from octopus.llm.router import LLMRouter
from octopus.models import Attachment, IncomingMessage
from octopus.settings import Settings


def make_app(tmp_path, fetcher=None, channel=None, answer_reply="复利需要时间 [1]。"):
    cfg = make_test_config(tmp_path)
    cfg.channels["tg"] = ChannelConfig(enabled=True, allowed_users=["me"])
    conn = connect(cfg.db_path, embed_dim=FakeEmbedder.dim)
    lib = Library(cfg.library_dir, cfg.trash_dir)
    idx = Index(conn, FakeEmbedder())
    providers = {
        "claude:haiku": FakeProvider("claude:haiku", summary_json(title="复利的力量", tags=("投资",))),
        "claude:sonnet": FakeProvider("claude:sonnet", answer_reply),
        "local": FakeProvider("local", "本地回答"),
    }
    llm = LLMRouter(providers, {"summarize": cfg.summarize, "answer": cfg.answer}, Settings(conn))
    ch = channel or FakeChannel("tg")
    reg = FetcherRegistry([fetcher] if fetcher else [])
    app = App(cfg, conn=conn, library=lib, index=idx, llm=llm, registry=reg, channels={ch.name: ch})
    return app, ch, providers


def m(text, user="me", channel="tg", attachments=None):
    return IncomingMessage(channel=channel, user_id=user, text=text, attachments=attachments or [])


ARTICLE = FetchResult(url="https://a.com/x", source="web", title="复利的力量", text="长期主义的投资者相信复利。" * 30)


async def test_link_then_question_end_to_end(tmp_path):
    app, ch, providers = make_app(tmp_path, StubFetcher(ARTICLE))
    await app.handle(m("看看 https://a.com/x?utm_source=wx"))
    assert ch.sent == [("me", "收到，处理中…")]
    await app.process_due_jobs()
    saved = ch.sent[-1][1]
    assert saved.startswith("✅ 已保存：复利的力量") and "#投资" in saved

    await app.handle(m("复利有什么用？"))
    reply = ch.sent[-1][1]
    assert reply.startswith("复利需要时间 [1]。")
    eid = app.index.recent(1)[0]["id"]
    assert f"[1] 复利的力量（id：{eid}） https://a.com/x" in reply
    assert app.convos.recent("tg", "me") == [("user", "复利有什么用？"), ("assistant", "复利需要时间 [1]。")]


async def test_duplicate_link(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    await app.handle(m("https://a.com/x?utm_source=again"))
    assert ch.sent[-1][1].startswith("这个链接已经存过了：复利的力量")
    assert app.jobs.counts() == {}


async def test_focus_follows_saved_entry_and_duplicate(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    assert app.focus.get("tg", "me") is None
    await app.handle(m("/s 今天读到一个观点：复利是第八大奇迹"))
    await app.process_due_jobs()
    note_id = app.focus.get("tg", "me")
    assert note_id and app.library.get(note_id) is not None
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    link_id = app.focus.get("tg", "me")
    assert link_id and link_id != note_id
    await app.handle(m("/s 又一条笔记：复利很重要"))
    await app.process_due_jobs()
    assert app.focus.get("tg", "me") not in (note_id, link_id)
    await app.handle(m("https://a.com/x?utm_source=again"))  # duplicate, answered at enqueue time
    assert app.focus.get("tg", "me") == link_id
    assert app.focus.get("tg", "other") is None


async def test_note_saved(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/s 今天读到一个观点：复利是第八大奇迹"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存：复利的力量")


async def test_no_ack_when_channel_does_not_ack(tmp_path):
    ch = FakeChannel("tg", acks_saves=False)
    app, _, _ = make_app(tmp_path, StubFetcher(ARTICLE), channel=ch)
    await app.handle(m("https://a.com/x"))
    assert ch.sent == []
    await app.process_due_jobs()
    assert len(ch.sent) == 1


async def test_disallowed_user_ignored(tmp_path):
    app, ch, providers = make_app(tmp_path)
    await app.handle(m("复利有什么用？", user="stranger"))
    await app.handle(m("hi", channel="unknown"))
    assert ch.sent == []
    assert providers["claude:sonnet"].calls == []


async def test_command(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/model local"))
    assert ch.sent[-1][1].startswith("已切换到本地模型")


async def test_retryable_fetch_error_reschedules(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(FetchError("网络错误", retryable=True)))
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    assert app.jobs.counts() == {"queued": 1}
    assert ch.sent == [("me", "收到，处理中…"), ("me", "暂时没读到内容（网络错误），我过一会儿自动再试，读到了会告诉你。")]


async def test_retry_notice_is_sent_once(tmp_path, caplog):
    app, ch, _ = make_app(tmp_path, StubFetcher(FetchError("页面里找不到正文", retryable=True)))
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    app.conn.execute("UPDATE jobs SET next_run_at = 0")  # the first retry is due
    await app.process_due_jobs()
    assert app.jobs.counts() == {"queued": 1}
    assert [t for _, t in ch.sent].count("暂时没读到内容（页面里找不到正文），我过一会儿自动再试，读到了会告诉你。") == 1
    assert "could not fetch a.com:" in caplog.text and "页面里找不到正文" in caplog.text  # the host only (P16)


async def test_permanent_fetch_error_saves_needs_manual(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(FetchError("网站拒绝访问（HTTP 403）", retryable=False)))
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    text = ch.sent[-1][1]
    assert text.startswith("⚠️ 没能获取内容：https://a.com/x")
    assert "HTTP 403" in text
    assert app.index.find_by_url("https://a.com/x")


async def test_question_when_all_models_down(tmp_path):
    app, ch, providers = make_app(tmp_path, StubFetcher(ARTICLE))
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    providers["claude:sonnet"].error = "limit"
    providers["local"].error = "down"
    await app.handle(m("复利有什么用？"))
    assert ch.sent[-1][1].startswith("现在没法回答：模型都不可用")


async def test_worker_loop_processes_and_stops(tmp_path):
    import asyncio

    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    await app.handle(m("https://a.com/x"))
    for _ in range(50):
        if len(ch.sent) == 2:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(worker, 3)
    assert ch.sent[-1][1].startswith("✅ 已保存")


async def test_handle_error_boundary(tmp_path):
    app, ch, _ = make_app(tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("boom")

    app.commands.reply = boom
    await app.handle(m("/model local"))
    assert ch.sent[-1] == ("me", "处理出错了，已记录日志。")


async def test_run_job_is_total_when_save_failed_url_raises(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(FetchError("拒绝", retryable=False)))

    async def boom(*a, **k):
        raise RuntimeError("disk full")

    app.pipeline.save_failed_url = boom
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    assert app.jobs.counts() == {"queued": 1}


async def test_run_job_failure_notifies_when_out_of_retries(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(FetchError("拒绝", retryable=False)))

    async def boom(*a, **k):
        raise RuntimeError("disk full")

    app.pipeline.save_failed_url = boom
    app.jobs.retry_or_fail = lambda *a, **k: False
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    assert "disk full" in ch.sent[-1][1]


async def test_worker_survives_claim_error(tmp_path):
    import asyncio

    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    real = app.jobs.claim_due
    calls = {"n": 0}

    def flaky(limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db locked")
        return real(limit)

    app.jobs.claim_due = flaky
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    await app.handle(m("https://a.com/x"))
    for _ in range(100):
        if len(ch.sent) == 2:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(worker, 3)
    assert ch.sent[-1][1].startswith("✅ 已保存")


async def test_in_flight_url_not_enqueued_twice(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    await app.handle(m("https://a.com/x"))
    await app.handle(m("https://a.com/x?utm_source=again"))
    assert ch.sent[-1][1] == "这个链接正在处理中，请稍等。"
    assert app.jobs.counts() == {"queued": 1}
    await app.process_due_jobs()
    assert len(app.index.recent(10)) == 1


async def test_execution_time_dedupe(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    app.cfg.max_concurrent_jobs = 1
    for _ in range(2):
        app.jobs.enqueue("url", {"url": "https://a.com/x"}, "tg", "me")
    await app.process_due_jobs()
    assert len(app.index.recent(10)) == 1
    assert ch.sent[0][1].startswith("✅ 已保存")
    assert ch.sent[1][1].startswith("这个链接已经存过了")
    assert app.jobs.counts() == {}


async def test_stop_waits_for_in_flight_job(tmp_path):
    import asyncio
    import threading

    release = threading.Event()

    class SlowFetcher(StubFetcher):
        def fetch(self, url):
            release.wait(5)
            return super().fetch(url)

    app, ch, _ = make_app(tmp_path, SlowFetcher(ARTICLE))
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    await app.handle(m("https://a.com/x"))
    for _ in range(50):
        if app.jobs.counts() == {"running": 1}:
            break
        await asyncio.sleep(0.05)
    assert app.jobs.counts() == {"running": 1}
    stop.set()
    await asyncio.sleep(0.2)
    assert not worker.done()
    release.set()
    await asyncio.wait_for(worker, 3)
    assert ch.sent[-1][1].startswith("✅ 已保存")


async def test_run_worker_survives_recover_failure(tmp_path):
    import asyncio

    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))

    def boom():
        raise RuntimeError("db locked")

    app.jobs.recover = boom
    app.jobs.recover_failed = boom  # P4: run_worker recovers through recover_failed()
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    await app.handle(m("https://a.com/x"))
    for _ in range(100):
        if any(t.startswith("✅") for _, t in ch.sent):
            break
        await asyncio.sleep(0.05)
    stop.set()
    app.wake.set()
    await asyncio.wait_for(worker, 5)
    assert any(t.startswith("✅ 已保存") for _, t in ch.sent)


async def test_index_failure_then_retry_leaves_one_entry(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    real = app.index.upsert
    calls = {"n": 0}

    async def flaky(entry, path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("index down")
        return await real(entry, path)

    app.index.upsert = flaky
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    assert app.jobs.counts() == {"queued": 1}
    app.conn.execute("UPDATE jobs SET next_run_at = 0")
    await app.process_due_jobs()
    assert len(list(app.library.root.glob("*/*/*.md"))) == 1
    assert app.index.count() == 1
    assert ch.sent[-1][1].startswith("✅ 已保存")


async def test_duplicate_url_with_deleted_markdown_is_saved_again(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    old_id = app.index.find_by_url("https://a.com/x")
    app.library.find(old_id).unlink()  # deleted by hand
    await app.handle(m("https://a.com/x"))
    assert not ch.sent[-1][1].startswith("这个链接已经存过了")
    assert app.jobs.counts() == {"queued": 1}
    await app.process_due_jobs()
    new_id = app.index.find_by_url("https://a.com/x")
    assert new_id and new_id != old_id and app.library.get(new_id) is not None
    assert app.index.count() == 1


async def test_execution_time_dedupe_ignores_stale_index_row(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    old_id = app.index.find_by_url("https://a.com/x")
    app.library.find(old_id).unlink()
    app.jobs.enqueue("url", {"url": "https://a.com/x"}, "tg", "me")
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存")
    new_id = app.index.find_by_url("https://a.com/x")
    assert new_id != old_id and app.library.get(new_id) is not None


async def test_run_worker_backfills_vectors_in_background(tmp_path):
    import asyncio

    app, ch, _ = make_app(tmp_path)
    app.index.embedder.fail = True
    await app.pipeline.save_note("一条笔记", "tg")
    assert len(app.index.missing_vectors()) == 1
    app.index.embedder.fail = False
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    for _ in range(100):
        if not app.index.missing_vectors():
            break
        await asyncio.sleep(0.05)
    stop.set()
    app.wake.set()
    await asyncio.wait_for(worker, 5)
    assert app.index.missing_vectors() == []


async def test_run_worker_retries_backfill_periodically(tmp_path):
    import asyncio

    app, ch, _ = make_app(tmp_path)
    app.backfill_interval = 0.1
    app.index.embedder.fail = True
    await app.pipeline.save_note("一条笔记", "tg")
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    await asyncio.sleep(0.3)
    assert len(app.index.missing_vectors()) == 1  # embedder still down: first run gave up
    app.index.embedder.fail = False  # Ollama comes back later
    for _ in range(100):
        if not app.index.missing_vectors():
            break
        await asyncio.sleep(0.05)
    stop.set()
    app.wake.set()
    await asyncio.wait_for(worker, 5)
    assert app.index.missing_vectors() == []


async def test_backfill_retry_is_rate_limited(tmp_path):
    import asyncio

    app, ch, _ = make_app(tmp_path)
    app.index.embedder.fail = True
    await app.pipeline.save_note("一条笔记", "tg")
    runs = []
    real = app.pipeline.backfill_vectors

    async def counted():
        runs.append(1)
        return await real()

    app.pipeline.backfill_vectors = counted
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    await asyncio.sleep(1.5)  # several loop iterations, default 10 min interval
    stop.set()
    app.wake.set()
    await asyncio.wait_for(worker, 5)
    assert len(runs) == 1


async def test_disallowed_user_attachments_are_deleted(tmp_path):
    app, ch, _ = make_app(tmp_path)
    staging = app.cfg.incoming_dir / "abc123"
    staging.mkdir(parents=True)
    f = staging / "x.pdf"
    f.write_bytes(b"data")
    outside = tmp_path / "keep.txt"
    outside.write_text("mine")
    atts = [Attachment(kind="file", path=f, filename="x.pdf"), Attachment(kind="file", path=outside, filename="keep.txt")]
    await app.handle(m("", user="stranger", attachments=atts))
    assert not f.exists() and not staging.exists()
    assert outside.exists()  # only files staged under incoming_dir are ever removed
    assert ch.sent == []


async def test_worker_backs_up_daily_and_survives_backup_failure(tmp_path, monkeypatch):
    import asyncio
    from datetime import date

    import octopus.app as app_mod

    app, _, _ = make_app(tmp_path)
    app.today = lambda: date(2026, 10, 13)
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    target = app.cfg.backups_dir / "octopus-20261013.db"
    for _ in range(60):
        if target.exists():
            break
        await asyncio.sleep(0.05)
    assert target.exists()
    stop.set()
    await asyncio.wait_for(worker, 3)

    # a failing backup is logged, never fatal to the worker
    (tmp_path / "two").mkdir()
    app2, ch2, _ = make_app(tmp_path / "two", StubFetcher(ARTICLE))
    monkeypatch.setattr(app_mod, "backup_db", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    stop2 = asyncio.Event()
    worker2 = asyncio.create_task(app2.run_worker(stop2))
    await app2.handle(m("https://a.com/x"))
    for _ in range(60):
        if len(ch2.sent) == 2:
            break
        await asyncio.sleep(0.05)
    stop2.set()
    await asyncio.wait_for(worker2, 3)
    assert ch2.sent[-1][1].startswith("✅ 已保存")


async def test_run_worker_tells_users_about_jobs_failed_by_repeated_crashes(tmp_path):
    import asyncio

    from octopus.formatting import format_job_failed
    from octopus.jobs import RETRY_DELAYS

    app, ch, _ = make_app(tmp_path)
    jid = app.jobs.enqueue("url", {"url": "https://a.com/x"}, "tg", "me")
    for _ in range(len(RETRY_DELAYS)):
        app.jobs.claim_due(1)
        app.jobs.recover()
    app.jobs.claim_due(1)  # running when the process died once more
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    for _ in range(100):
        if ch.sent:
            break
        await asyncio.sleep(0.02)
    stop.set()
    app.wake.set()
    await asyncio.wait_for(worker, 5)
    assert ch.sent == [("me", format_job_failed("程序在处理这个任务时多次中断"))]
    assert app.jobs.get(jid).status == "failed"


async def test_a_link_urllib_cannot_parse_is_chat_not_an_error(tmp_path):
    # P19: urlsplit raised on the bracket, the user got the generic error reply
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("看看 http://[abc/def"))
    assert ch.sent[-1][1].startswith("复利需要时间")
    assert app.jobs.claim_due(10) == []


async def test_a_link_saved_before_p19_without_its_paren_is_still_a_duplicate(tmp_path):
    # P19 fix round 1 (M7): such entries are stored without the ")" the link ends with
    wiki = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    app.conn.execute("UPDATE entries SET url = ?", (wiki[:-1],))
    await app.handle(m(wiki))
    assert ch.sent[-1][1].startswith("这个链接已经存过了：复利的力量")
    assert app.jobs.counts() == {}


# ---- P21 integration fix round 1 (I1): a note renamed in Obsidian since the last scan is not a stale row ----

def _rename(app, eid):
    """Rename the entry's file the way Obsidian would; no scan runs, so only a walk finds it."""
    path = app.library.find(eid)
    new = path.with_name("改了名字.md")
    path.rename(new)
    return new


async def test_a_link_sent_again_after_a_rename_is_still_a_duplicate(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    [(eid, _)] = app.index.titles()
    _rename(app, eid)
    calls = app.index.embedder.calls
    await app.handle(m("https://a.com/x"))
    assert ch.sent[-1][1].startswith("这个链接已经存过了：复利的力量")
    assert app.index.path_of(eid) is not None and app.jobs.counts() == {}
    await app.process_due_jobs()
    assert [e for e, _ in app.index.titles()] == [eid] and app.index.embedder.calls == calls


async def test_a_needs_manual_note_renamed_is_refetched_not_saved_again(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    eid = (await app.pipeline.save_failed_url("https://a.com/x", "tg", "读不出来")).entry.id
    new = _rename(app, eid)
    calls = app.index.embedder.calls
    await app.handle(m("https://a.com/x"))
    assert [j.kind for j in app.jobs.claim_due(5)] == ["refetch"]
    app.conn.execute("UPDATE jobs SET status = 'queued'")
    await app.process_due_jobs()
    assert [e for e, _ in app.index.titles()] == [eid] and app.index.path_of(eid) == str(new)
    assert app.library.get(eid).status == "ok" and "复利" in new.read_text(encoding="utf-8")
    assert app.index.embedder.calls == calls + 1  # the refetched text, once


async def test_a_refetch_queued_before_a_rename_fills_in_the_renamed_note(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    eid = (await app.pipeline.save_failed_url("https://a.com/x", "tg", "读不出来")).entry.id
    await app.handle(m("https://a.com/x"))  # the refetch job is queued
    new = _rename(app, eid)
    await app.process_due_jobs()
    assert [e for e, _ in app.index.titles()] == [eid]
    assert app.library.get(eid).status == "ok" and "复利" in new.read_text(encoding="utf-8")


async def test_a_url_job_finds_the_entry_saved_meanwhile_even_when_renamed(tmp_path):
    app, ch, _ = make_app(tmp_path, StubFetcher(ARTICLE))
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    [(eid, _)] = app.index.titles()
    _rename(app, eid)
    calls = app.index.embedder.calls
    app.jobs.enqueue("url", {"url": "https://a.com/x"}, "tg", "me")  # queued before the first one was saved
    await app.process_due_jobs()
    assert [e for e, _ in app.index.titles()] == [eid] and app.index.embedder.calls == calls
