from datetime import datetime, timedelta, timezone

from fakes import FakeEmbedder, FakeProvider, summary_json

from octopus.commands import AMBIGUOUS_ID, DELETE_USAGE, GUIDE_BUTTONS, HELP, SAVE_USAGE, Commands, ambiguous_block
from octopus.config import TaskModels
from octopus.db import connect
from octopus.fetchers import FetcherRegistry
from octopus.index import Index
from octopus.jobs import JobQueue
from octopus.library import Entry, Library
from octopus.llm.router import MODEL_MODE_KEY, LLMRouter
from octopus.pipeline import SavePipeline
from octopus.settings import Settings

CST = timezone(timedelta(hours=8))


def make(tmp_path):
    conn = connect(tmp_path / "c.db", embed_dim=FakeEmbedder.dim)
    settings = Settings(conn)
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    idx = Index(conn, FakeEmbedder())
    providers = {n: FakeProvider(n, summary_json()) for n in ("claude:haiku", "claude:sonnet", "local")}
    llm = LLMRouter(providers, {"summarize": TaskModels("claude:haiku", "local"),
                                "answer": TaskModels("claude:sonnet", "local")}, settings)
    jobs = JobQueue(conn)
    pipe = SavePipeline(lib, idx, llm, FetcherRegistry([]), clock=lambda: datetime(2026, 10, 13, tzinfo=CST))
    return Commands(llm, settings, lib, idx, jobs, pipe, tmp_path), pipe, settings, jobs


async def test_help_and_unknown(tmp_path):
    cmds, *_ = make(tmp_path)
    assert await cmds.run("help", []) == HELP
    assert (await cmds.run("bogus", [])).startswith("没有这个命令：/bogus")
    for name in ("/s 内容", "/model", "/model local", "/model default", "/recent", "/delete", "/resummarize", "/status",
                 "/clawbot", "/help"):
        assert name in HELP
    assert HELP.startswith("你好！我是 Octopus，你的个人知识库。\n\n【存东西】")
    assert "/delete id … — 删除一条或多条，例：/delete 20261013-73b48c 20261013-905eeb" in HELP
    assert HELP.endswith("/help — 再看一遍这个说明")


async def test_save_without_content_shows_usage(tmp_path):
    cmds, *_ = make(tmp_path)
    assert await cmds.run("save", []) == SAVE_USAGE == "用法：/s 内容（也可以写“存 内容”或“存：内容”）"


async def test_model_switching(tmp_path):
    cmds, _, settings, _ = make(tmp_path)
    assert await cmds.run("model", []) == "当前模型：\n总结：claude:haiku（出错时改用 local）\n回答：claude:sonnet（出错时改用 local）"
    assert (await cmds.run("model", ["local"])).startswith("已切换到本地模型")
    assert settings.get(MODEL_MODE_KEY) == "local"
    assert "总结：local\n回答：local" in await cmds.run("model", [])
    assert (await cmds.run("model", ["default"])).startswith("已恢复默认模型")
    assert settings.get(MODEL_MODE_KEY) is None
    assert (await cmds.run("model", ["x"])).startswith("用法")


async def test_recent_delete_resummarize(tmp_path):
    cmds, pipe, _, _ = make(tmp_path)
    assert await cmds.run("recent", []) == "还没有保存任何内容。"
    out = await pipe.save_note("一条笔记", "cli")
    eid = out.entry.id
    assert f"{eid}  标题" in await cmds.run("recent", [])
    assert (await cmds.run("resummarize", [eid])).startswith("✅ 已保存：")
    assert await cmds.run("resummarize", ["nope"]) == "找不到这一条：nope"
    assert (await cmds.run("delete", [])).startswith(f"选择要删除的一条：\n{eid}  标题")
    assert await cmds.run("delete", [eid]) == f"已删除：「标题」 {eid}\n（文件已移到回收目录）"
    assert await cmds.run("delete", [eid]) == f"找不到：{eid}"
    assert cmds.index.count() == 0


async def test_delete_goes_through_pipeline(tmp_path):
    cmds, pipe, _, _ = make(tmp_path)
    eid = (await pipe.save_note("一条笔记", "cli")).entry.id
    seen = []
    original = pipe.delete

    async def spy(entry_id):
        seen.append(entry_id)
        return await original(entry_id)

    pipe.delete = spy
    assert await cmds.run("delete", [eid]) == f"已删除：「标题」 {eid}\n（文件已移到回收目录）"
    assert await cmds.run("delete", ["../x"]) == "找不到：../x"
    assert seen == [eid]


async def test_status(tmp_path):
    cmds, pipe, _, jobs = make(tmp_path)
    await pipe.save_note("一条笔记", "cli")
    jobs.enqueue("url", {"url": "x"}, "cli", "u")
    text = await cmds.run("status", [])
    assert "已保存：1 条" in text
    assert "排队中：1" in text
    assert "总结：claude:haiku（出错时改用 local）" in text
    assert "占用空间：" in text


async def test_invalid_ids_touch_nothing(tmp_path):
    cmds, pipe, _, _ = make(tmp_path)
    out = await pipe.save_note("一条笔记", "cli")
    eid = out.entry.id
    for bad in ("*", eid[:8], "../x", eid + "*"):
        assert await cmds.run("delete", [bad]) == f"找不到：{bad}"
        assert await cmds.run("resummarize", [bad]) == f"找不到这一条：{bad}"
    assert cmds.index.count() == 1
    assert cmds.library.get(eid) is not None


async def test_resummarize_failure_is_reported(tmp_path):
    cmds, pipe, _, _ = make(tmp_path)
    eid = (await pipe.save_note("一条笔记", "cli")).entry.id
    for p in cmds.llm.providers.values():
        p.error = "down"
    text = await cmds.run("resummarize", [eid])
    assert text.startswith("⚠️ 重新生成摘要失败：标题")
    assert "/resummarize" in text and f"id：{eid}" in text
    assert "✅" not in text


async def test_status_reports_missing_vectors(tmp_path):
    cmds, pipe, _, _ = make(tmp_path)
    cmds.index.embedder.fail = True
    await pipe.save_note("一条笔记", "cli")
    assert "缺少向量：1 条" in await cmds.run("status", [])


# ---- P4 Task 5: /clawbot ----

class FakeClawBot:
    def __init__(self, connected):
        self.connected, self.requests = connected, 0

    def request_login(self) -> bool:
        if self.connected:
            return False
        self.requests += 1
        return True


async def test_clawbot_command(tmp_path):
    cmds, *_ = make(tmp_path)
    assert "/clawbot" in HELP
    assert await cmds.run("clawbot", []) == "微信 ClawBot 没有启用。"  # e.g. `octopus chat`: no reference
    cmds.clawbot = FakeClawBot(connected=True)
    assert await cmds.run("clawbot", []) == "微信 ClawBot 现在是连接状态，不需要扫码。"
    cmds.clawbot = bot = FakeClawBot(connected=False)
    assert await cmds.run("clawbot", []) == "已生成新的微信 ClawBot 二维码，稍后发给你。"
    assert bot.requests == 1


GUIDE = [("最近存的", "cmd:recent"), ("今天存的", "cmd:list"), ("运行状态", "cmd:status"), ("当前模型", "cmd:model"),
         ("切到本机模型", "cmd:model local"), ("恢复默认模型", "cmd:model default"), ("删除一条", "cmd:delete")]


async def test_help_reply_carries_the_guide_buttons(tmp_path):
    cmds, *_ = make(tmp_path)
    r = await cmds.reply("help", [])
    assert r.text == HELP
    assert [(b.label, b.data) for b in r.buttons] == GUIDE
    assert all(b.fallback == "" for b in r.buttons)
    assert [(b.label, b.data) for b in GUIDE_BUTTONS] == GUIDE
    assert (await cmds.reply("model", [])).buttons == []


async def test_delete_without_ids_is_a_picker(tmp_path):
    cmds, pipe, _, _ = make(tmp_path)
    r = await cmds.reply("delete", [])
    assert r.text == "还没有保存任何内容。" and r.buttons == []
    ids = []
    for i in range(12):
        pipe.llm.providers["claude:haiku"].respond = summary_json(title=f"第{i}条笔记的一个很长很长的标题")
        ids.append((await pipe.save_note(f"笔记 {i}", "cli")).entry.id)
    r = await cmds.reply("del", [])
    shown = [row["id"] for row in cmds.index.recent(10)]
    assert len(shown) == 10
    assert r.text == "选择要删除的一条：\n" + "\n".join(f"{i}  {cmds.library.get(i).title}" for i in shown)
    assert [b.data for b in r.buttons] == [f"del:{i}" for i in shown]
    title = cmds.library.get(shown[0]).title
    assert r.buttons[0].label == f"删除 {title[:12]}"
    # ClawBot shows no buttons: only the usage hint is added to the list there
    assert [b.fallback for b in r.buttons] == [""] * 9 + [DELETE_USAGE]
    assert all(cmds.library.get(i) is not None for i in ids)  # listing deletes nothing


async def test_delete_several_ids(tmp_path):
    cmds, pipe, _, _ = make(tmp_path)
    titles = {}
    for t in ("甲", "乙", "丙"):
        pipe.llm.providers["claude:haiku"].respond = summary_json(title=t)
        titles[t] = (await pipe.save_note(f"笔记{t}", "cli")).entry.id
    a, b, c = titles["甲"], titles["乙"], titles["丙"]
    deleted = []
    original = pipe.delete

    async def spy(entry_id):
        ok = await original(entry_id)
        if ok:
            deleted.append(entry_id)
        return ok

    pipe.delete = spy
    text = await cmds.run("delete", [a, f"{b},20261013-ffffff，{c}", a, "x*"])
    assert text == (f"已删除：「甲」 {a}\n已删除：「乙」 {b}\n找不到：20261013-ffffff\n已删除：「丙」 {c}\n"
                    "找不到：x*\n（文件已移到回收目录）")
    assert deleted == [a, b, c] and cmds.index.count() == 0


async def test_del_is_an_alias_of_delete(tmp_path):
    cmds, pipe, _, _ = make(tmp_path)
    eid = (await pipe.save_note("一条笔记", "cli")).entry.id
    assert await cmds.run("del", [eid]) == f"已删除：「标题」 {eid}\n（文件已移到回收目录）"
    assert await cmds.run("del", [eid, "20261013-ffffff"]) == f"找不到：{eid}\n找不到：20261013-ffffff"


async def test_delete_survives_a_corrupt_entry(tmp_path):
    cmds, pipe, _, _ = make(tmp_path)
    a = (await pipe.save_note("一条笔记", "cli")).entry.id
    pipe.llm.providers["claude:haiku"].respond = summary_json(title="另一条")
    c = (await pipe.save_note("又一条", "cli")).entry.id
    bad = "20261013-abcdef"
    corrupt = cmds.library.root / "2026" / "10" / f"{bad}-坏了.md"
    corrupt.write_text("没有 frontmatter 的文件", encoding="utf-8")
    text = await cmds.run("delete", [a, bad, c])
    assert text == f"已删除：「标题」 {a}\n已删除：{bad}\n已删除：「另一条」 {c}\n（文件已移到回收目录）"
    assert not corrupt.exists() and (cmds.library.trash / corrupt.name).exists()
    assert cmds.library.get(a) is None and cmds.library.get(c) is None


async def test_delete_with_only_separators_is_the_picker(tmp_path):
    cmds, pipe, _, _ = make(tmp_path)
    for sep in (",", "，", ", ，"):
        assert await cmds.run("delete", [sep]) == "还没有保存任何内容。"
    eid = (await pipe.save_note("一条笔记", "cli")).entry.id
    r = await cmds.reply("delete", ["，"])
    assert r.text == f"选择要删除的一条：\n{eid}  标题" and [b.data for b in r.buttons] == [f"del:{eid}"]
    assert cmds.library.get(eid) is not None


async def test_start_is_an_alias_of_help(tmp_path):
    cmds, _, _, _ = make(tmp_path)
    reply = await cmds.reply("start", [])
    assert reply.text == HELP and reply.buttons == GUIDE_BUTTONS


def _put(cmds, eid, title):
    cmds.library.save(Entry(id=eid, title=title, source="note", saved_at=datetime(2026, 10, 13, tzinfo=CST),
                            channel="cli", summary="摘要"))


async def test_delete_by_unique_short_id(tmp_path):
    cmds, *_ = make(tmp_path)
    _put(cmds, "20261013-6e43c1", "甲")
    _put(cmds, "20261013-aaaaaa", "乙")
    assert await cmds.run("delete", ["43C1"]) == "已删除：「甲」 20261013-6e43c1\n（文件已移到回收目录）"
    assert cmds.library.get("20261013-6e43c1") is None and cmds.library.get("20261013-aaaaaa")


async def test_delete_ambiguous_short_id_deletes_neither(tmp_path):
    cmds, *_ = make(tmp_path)
    _put(cmds, "20261013-9eb123", "甲")
    _put(cmds, "20261012-aaa123", "乙")
    text = await cmds.run("delete", ["123"])
    assert text == AMBIGUOUS_ID.format(token="123") + "\n20261013-9eb123  甲\n20261012-aaa123  乙"
    assert cmds.library.get("20261013-9eb123") and cmds.library.get("20261012-aaa123")


async def test_delete_mixed_list_of_short_ids(tmp_path):
    cmds, *_ = make(tmp_path)
    _put(cmds, "20261013-6e43c1", "甲")
    _put(cmds, "20261013-aaa777", "乙")
    _put(cmds, "20261012-bbb777", "丙")
    text = await cmds.run("delete", ["3c1", "777,zzz9", "20261013-6e43c1", "fff"])
    assert text == ("已删除：「甲」 20261013-6e43c1\n" + AMBIGUOUS_ID.format(token="777")
                    + "\n20261013-aaa777  乙\n20261012-bbb777  丙\n找不到：zzz9\n找不到：fff\n（文件已移到回收目录）")


def test_ambiguous_block_lists_at_most_ten(tmp_path):
    cmds, *_ = make(tmp_path)
    for n in range(12):
        _put(cmds, f"202610{n + 1:02d}-a{n:02x}abc", f"标题{n}")
    lines = ambiguous_block(cmds.library, "abc", cmds.library.match_short("abc")).splitlines()
    assert len(lines) == 11 and lines[1].startswith("20261012-")


async def test_resummarize_by_short_id_and_ambiguous(tmp_path):
    cmds, *_ = make(tmp_path)
    _put(cmds, "20261013-6e43c1", "甲")
    _put(cmds, "20261013-ccc3c1", "乙")
    assert (await cmds.run("resummarize", ["43c1"])).startswith("✅ 已保存：")
    text = await cmds.run("resummarize", ["3c1"])
    assert text.startswith(AMBIGUOUS_ID.format(token="3c1")) and "20261013-ccc3c1  乙" in text
    assert await cmds.run("resummarize", ["fff"]) == "找不到这一条：fff"


# ---- P8: /recent N and /list <date> ----

async def _saved(cmds, eid, title, when, status="ok"):
    entry = Entry(id=eid, title=title, source="note", saved_at=when, channel="cli", summary="摘要", status=status)
    await cmds.index.upsert(entry, cmds.library.save(entry))


async def test_recent_takes_a_count(tmp_path):
    from octopus.commands import RECENT_MAX, RECENT_USAGE

    cmds, *_ = make(tmp_path)
    for i in range(60):
        await _saved(cmds, f"20261013-{i:06x}", f"第{i}条", datetime(2026, 10, 13, 8, i % 60, tzinfo=CST))
    assert len((await cmds.run("recent", [])).splitlines()) == 1 + 10
    text = await cmds.run("recent", ["30"])
    assert text.startswith("最近保存：\n20261013-00003b  第59条") and len(text.splitlines()) == 1 + 30
    assert len((await cmds.run("recent", ["３"])).splitlines()) == 1 + 3  # full-width digits
    text = await cmds.run("recent", ["80"])
    assert RECENT_MAX == 50 and len(text.splitlines()) == 1 + 50 + 1 and text.endswith("\n最多显示 50 条。")
    for bad in (["0"], ["-3"], ["abc"], ["3.5"]):
        assert await cmds.run("recent", bad) == RECENT_USAGE == "用法：/recent 或 /recent 30（最多 50）"



async def test_recent_cap_note_only_when_something_was_cut(tmp_path):
    cmds, *_ = make(tmp_path)
    for i in range(3):
        await _saved(cmds, f"20261013-{i:06x}", f"第{i}条", datetime(2026, 10, 13, 8, i, tzinfo=CST))
    assert "最多显示" not in await cmds.run("recent", ["80"])


async def test_list_shows_one_days_entries(tmp_path):
    from datetime import date

    from octopus.commands import LIST_USAGE

    cmds, *_ = make(tmp_path)
    cmds.today = lambda: date(2026, 10, 14)
    us = timezone(timedelta(hours=-4))
    await _saved(cmds, "20261014-aaaaaa", "早上", datetime(2026, 10, 14, 0, 30, tzinfo=us))
    await _saved(cmds, "20261014-bbbbbb", "晚上", datetime(2026, 10, 14, 23, 50, tzinfo=us), status="needs_manual")
    await _saved(cmds, "20261015-cccccc", "第二天", datetime(2026, 10, 15, 0, 10, tzinfo=us))  # 04:10Z on the 15th
    await _saved(cmds, "20261013-dddddd", "前一天", datetime(2026, 10, 13, 23, 0, tzinfo=us))  # 03:00Z on the 14th
    want = "2026-10-14 保存的记录（2 条）：\n20261014-bbbbbb  晚上（待补充）\n20261014-aaaaaa  早上"
    for arg in ("2026-10-14", "20261014", "2026/10/14", "2026.10.14", "２０２６-１０-１４"):
        assert await cmds.run("list", [arg]) == want
    assert await cmds.run("list", []) == want  # today, by the app's clock
    assert (await cmds.run("list", ["2026-10-15"])).startswith("2026-10-15 保存的记录（1 条）：")
    assert await cmds.run("list", ["2026-10-01"]) == "2026-10-01 没有保存的记录。"
    assert await cmds.run("list", ["2026-1-5"]) == "2026-01-05 没有保存的记录。"
    for bad in (["2026-13-01"], ["2026-02-30"], ["昨天"], ["2026101"], ["2026-10-14", "x"]):
        assert await cmds.run("list", bad) == LIST_USAGE == "日期格式：/list 2026-10-14 或 /list 20261014"


async def test_help_lists_recent_and_list(tmp_path):
    from octopus.channels.telegram import COMMAND_MENU

    assert "/recent [数字]" in HELP and "/list [日期]" in HELP
    assert "5 分钟" in HELP and "/new" in HELP
    assert {"recent", "list"} <= {c for c, _ in COMMAND_MENU}
    cmds, *_ = make(tmp_path)
    assert (await cmds.reply("help", [])).buttons == GUIDE_BUTTONS
