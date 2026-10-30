from test_app_media import make_app, m, staged

import itertools
from datetime import datetime

import pytest

from octopus.library import Entry
from octopus.app import ADD_USAGE
from octopus.commands import NOTHING_SAVED


@pytest.fixture(autouse=True)
def digit_ids(monkeypatch):
    """Ids end in digits: the "add" alias (补充 <id>) needs one in the short id, and a random one may have none."""
    n = itertools.count(1)
    monkeypatch.setattr("octopus.pipeline.new_entry_id", lambda now: f"{now:%Y%m%d}-{next(n):06d}")


async def _entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    await app.handle(m("/done"))  # nothing open: /new would open a window that takes a text
    assert app.pending.opened_at("tg", "me") is None
    return app, ch, eid


async def test_add_opens_window_and_images_merge(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    await app.handle(m(f"/add {eid[-4:]}"))
    title = app.library.get(eid).title
    assert ch.sent[-1][1] == (f"好的，接下来发的图片、文件，还有第一段文字，都会加到：{title}\nid：{eid}\n"
                              "发“好了”、命令，或 5 分钟不发就结束。")
    assert app.focus.get("tg", "me") == eid
    for i in range(2):
        await app.handle(m("", [staged(tmp_path, f"s{i}.png")]))
        await app.process_due_jobs()
        assert ch.sent[-1][1].startswith("✅ 已补充到")
    assert app.index.count() == 1 and app.pending.active("tg", "me") == eid


async def test_add_with_text_merges_it_keeping_spacing(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    n = len(ch.sent)
    await app.handle(m(f"/add {eid[-4:]}  一段  补充文字 "))
    assert len(ch.sent) == n + 1  # only the ack, no window reply
    [job] = app.jobs.claim_due(10)
    assert job.kind == "merge" and job.payload["entry_id"] == eid and job.payload["text"] == "一段  补充文字"


async def test_add_with_text_end_to_end(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    await app.handle(m(f"/add {eid} 一段补充文字"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到")
    body = app.library.get(eid).body
    assert "补充内容" in body and "一段补充文字" in body
    assert app.pending.active("tg", "me") == eid


async def test_short_chat_text_in_window_is_still_chat_after_its_first_text(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    await app.handle(m(f"/add {eid[-4:]}"))
    await app.handle(m("一段补充"))  # the window's one text (P15)
    await app.handle(m("这是什么意思"))
    assert [j.payload["text"] for j in app.jobs.claim_due(10) if j.kind == "merge"] == ["一段补充"]


async def test_add_no_args_lists_recent(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    await app.handle(m("/add"))
    assert ch.sent[-1][1].startswith(ADD_USAGE + "\n") and eid in ch.sent[-1][1]


async def test_add_no_args_empty_library(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/add"))
    assert ch.sent[-1][1] == f"{ADD_USAGE}\n{NOTHING_SAVED}"


async def test_add_not_found_and_ambiguous(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    await app.handle(m("/add fff"))
    assert ch.sent[-1][1] == "找不到：fff"
    await app.handle(m("/add zzz"))
    assert ch.sent[-1][1] == "找不到：zzz"
    for i in ("20260101-aaa123", "20260202-bbb123"):
        app.library.save(Entry(id=i, saved_at=datetime(2026, 1, 1), title=f"T{i[-6:]}", body="x", source="note", channel="tg"))
    await app.handle(m("/add 123"))
    out = ch.sent[-1][1]
    assert out.startswith("「123」对应好几条记录，请多打几位：") and "20260101-aaa123  Taaa123" in out
    assert app.pending.active("tg", "me") is None


async def test_second_add_switches_target(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/2"))
    await app.process_due_jobs()
    other = app.pending.active("tg", "me")
    assert other and other != eid
    await app.handle(m(f"/add {eid}"))
    assert app.pending.active("tg", "me") == eid
    await app.handle(m(f"/add {other}"))
    assert app.pending.active("tg", "me") == other


async def test_new_ends_add_window(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    await app.handle(m(f"/add {eid}"))
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "x.png")]))
    assert [j.kind for j in app.jobs.claim_due(10)] == ["attachments"]


async def test_alias_adds_text(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    await app.handle(m(f"补充 {eid[-4:]} 文字  两格"))
    [job] = app.jobs.claim_due(10)
    assert job.kind == "merge" and job.payload["entry_id"] == eid and job.payload["text"] == "文字  两格"


@pytest.mark.parametrize("sep", ["，", ",", "：", ":", " ，", "： "])
async def test_punctuation_right_after_the_id(tmp_path, sep):
    app, ch, eid = await _entry(tmp_path)
    await app.handle(m(f"/add {eid[-4:]}{sep}一段文字"))
    [job] = app.jobs.claim_due(10)
    assert job.kind == "merge" and job.payload["entry_id"] == eid and job.payload["text"] == "一段文字"


async def test_multiline_text_keeps_its_lines(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    await app.handle(m(f"/add {eid[-4:]}\n第一行\n\n  第三行"))
    [job] = app.jobs.claim_due(10)
    assert job.payload["text"] == "第一行\n\n  第三行"
    await app.handle(m(f"补充 {eid[-4:]}\n甲\n乙"))
    [job] = app.jobs.claim_due(10)
    assert job.payload["text"] == "甲\n乙"


async def test_add_and_alias_with_attachments_merge_files(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    for text in (f"/add {eid[-4:]}", f"补充 {eid[-4:]}"):
        await app.handle(m(text, [staged(tmp_path, "a.png")]))
        [job] = app.jobs.claim_due(10)
        assert job.kind == "merge" and job.payload["entry_id"] == eid
        assert len(job.payload["files"]) == 1 and job.payload["text"] == ""


async def test_failed_add_discards_attachments(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    for text in ("/add", "/add fff", "/add zzz"):
        a = staged(tmp_path, "gone.png")
        await app.handle(m(text, [a]))
        assert not a.path.exists() and not app.jobs.claim_due(10)


async def test_alias_without_matching_entry_is_chat(tmp_path):
    app, ch, eid = await _entry(tmp_path)
    await app.handle(m("补充 2024 年的数据"))
    assert ch.sent[-1][1] != "找不到：2024" and not app.jobs.claim_due(10)
    assert app.convos.recent("tg", "me", 1)[0][1] == "补充 2024 年的数据"
    await app.handle(m("/add 2024"))
    assert ch.sent[-1][1] == "找不到：2024"
