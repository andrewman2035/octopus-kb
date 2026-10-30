"""P12: /read (an entry's full text in chat, in parts) and /find (exact words), no model calls."""
from datetime import datetime, timedelta, timezone

from fakes import FakeChannel
from test_app import m
from test_app import make_app as _make_app
from test_clawbot import FakeClient, text_msg
from test_clawbot import make as make_clawbot

from octopus.channels.base import SendResult
from octopus.channels.clawbot import MAX_TEXT, SEND_BUDGET
from octopus.channels.telegram import COMMAND_MENU
from octopus.app import PART_PAUSE_SECONDS
from octopus.commands import FIND_USAGE, FIND_WORDS_NONE, HELP, READ_PART_CHARS, READ_USAGE, read_parts
from octopus.config import ChannelConfig
from octopus.library import Entry
from octopus.qa import PINNED_MARK

CST = timezone(timedelta(hours=8))
OWNER = "owner@im.wechat"


def make_app(tmp_path, **kw):
    """The test app with the pause between /read parts recorded instead of slept."""
    app, ch, providers = _make_app(tmp_path, **kw)
    app.pauses = []

    async def pause(seconds):
        app.pauses.append(seconds)

    app.sleep = pause
    return app, ch, providers


async def seed(app, eid, title, body="", *, summary="", points=(), url=None, status="ok", notes="",
               when=datetime(2026, 10, 15, 9, 0, tzinfo=CST)):
    e = Entry(id=eid, title=title, source="web" if url else "manual", saved_at=when, channel="tg", status=status,
              url=url, summary=summary, points=list(points), body=body, notes=notes)
    path = app.library.save(e)
    await app.index.upsert(e, path)
    return e


def model_calls(app, providers):
    return sum(len(p.calls) for p in providers.values()), app.index.embedder.calls


def texts(ch):
    return [t for _, t in ch.sent]


# ---- /find ----

async def test_find_matches_short_chinese_and_english_words_with_snippets(tmp_path):
    app, ch, providers = make_app(tmp_path)
    await seed(app, "20261013-aaa111", "阳台菜园", "番茄架靠 Arduino 自动浇水。",
               when=datetime(2026, 10, 13, 9, tzinfo=CST))
    await seed(app, "20261014-bbb222", "家庭烘焙配方", "每周三次揉面。", summary="烘焙配方的关键是控制温度。",
               when=datetime(2026, 10, 14, 9, tzinfo=CST))
    await seed(app, "20261015-ccc333", "arduino 入门", "", summary="讲电路基础。", status="needs_manual",
               when=datetime(2026, 10, 15, 9, tzinfo=CST))
    before = model_calls(app, providers)

    await app.handle(m("/find 番茄架"))
    assert texts(ch)[-1] == ("含「番茄架」的记录（1 条）：\n20261013-aaa111  阳台菜园\n  番茄架靠 Arduino 自动浇水。\n"
                             "看全文：/read 记录号")
    await app.handle(m("/find ARDUINO"))  # case-insensitive; newest first; a title-only match has no snippet
    assert texts(ch)[-1] == ("含「ARDUINO」的记录（2 条）：\n20261015-ccc333  arduino 入门（待补充）\n"
                             "20261013-aaa111  阳台菜园\n  番茄架靠 Arduino 自动浇水。\n看全文：/read 记录号")
    await app.handle(m("/find 家庭"))  # 2-char word, in the title only
    assert texts(ch)[-1] == "含「家庭」的记录（1 条）：\n20261014-bbb222  家庭烘焙配方\n看全文：/read 记录号"
    await app.handle(m("/find 控制温度"))  # in the summary: the snippet comes from that part only
    assert texts(ch)[-1] == ("含「控制温度」的记录（1 条）：\n20261014-bbb222  家庭烘焙配方\n  烘焙配方的关键是控制温度。\n"
                             "看全文：/read 记录号")
    await app.handle(m("/find 揉面"))
    assert "\n  每周三次揉面。\n" in texts(ch)[-1]
    assert model_calls(app, providers) == before  # no model, no embedder


async def test_find_snippet_is_cut_around_the_first_match(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await seed(app, "20261013-aaa111", "长文", "前文" * 100 + "复利\n\n讲" + "后文" * 100)
    await app.handle(m("/find 复利"))
    assert texts(ch)[-1].split("\n")[2] == "  …" + "前文" * 5 + "复利 讲" + "后文" * 8 + "…"


async def test_find_needs_every_word_and_knows_quoted_phrases(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await seed(app, "20261013-aaa111", "阳台菜园", "番茄架靠 Arduino 自动浇水。")
    await seed(app, "20261014-bbb222", "家庭烘焙配方", "每周三次揉面，烘焙 配方要记牢。")
    await app.handle(m("/find 番茄架 arduino"))
    assert texts(ch)[-1].startswith("含「番茄架 arduino」的记录（1 条）：\n20261013-aaa111  阳台菜园")
    await app.handle(m("/find 番茄架 揉面"))
    assert texts(ch)[-1] == FIND_WORDS_NONE.format(q="番茄架 揉面") == "没找到含「番茄架 揉面」的记录。"
    await app.handle(m("/find 家庭 揉面"))  # one word in the title, one in the body
    assert "20261014-bbb222  家庭烘焙配方" in texts(ch)[-1]
    await app.handle(m('/find "烘焙 配方"'))  # a quoted phrase keeps its space
    assert "20261014-bbb222" in texts(ch)[-1] and "（1 条）" in texts(ch)[-1]
    await app.handle(m("/find “烘焙配方”"))
    assert "20261014-bbb222" in texts(ch)[-1]


async def test_find_substring_beats_word_segmentation(tmp_path):
    """jieba cuts 独立站 into 独立 / 站 and 站长寿命 into 站长 / 寿命: the scan matches the characters as typed
    (a word split by the tokenizer is still found)."""
    app, ch, _ = make_app(tmp_path)
    await seed(app, "20261013-aaa111", "建站", "我们靠独立站实现盈利")
    await seed(app, "20261014-bbb222", "随笔", "站长寿命不长")
    await app.handle(m("/find 独立站"))
    assert "20261013-aaa111" in texts(ch)[-1] and "20261014-bbb222" not in texts(ch)[-1]
    await app.handle(m("/find 长寿"))
    assert "20261014-bbb222" in texts(ch)[-1]


async def test_find_usage_none_and_cap(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/find"))
    assert texts(ch)[-1] == FIND_USAGE == "用法：/find 关键词，例：/find Reddit 独立站"
    await app.handle(m("/find 量子"))
    assert texts(ch)[-1] == "没找到含「量子」的记录。"
    for i in range(23):
        await seed(app, f"20261015-{i:06x}", f"第 {i} 篇", "说到复利。",
                   when=datetime(2026, 10, 15, 9, tzinfo=CST) + timedelta(minutes=i))
    await app.handle(m("/find 复利"))
    lines = texts(ch)[-1].split("\n")
    assert lines[0] == "含「复利」的记录（23 条）："
    assert lines[1] == "20261015-000016  第 22 篇"  # newest first
    assert sum(1 for x in lines if x.startswith("20261015-")) == 20
    assert lines[-2:] == ["还有 3 条，换个更具体的词试试。", "看全文：/read 记录号"]


async def test_find_snippet_is_short_and_fits_one_clawbot_message(tmp_path):
    app, ch, _ = make_app(tmp_path)
    for i in range(20):
        await seed(app, f"20261015-{i:06x}", "一个相当长的标题" * 3, "前文" * 100 + "复利" + "后文" * 100)
    await app.handle(m("/find 复利"))
    out = texts(ch)[-1]
    snippets = [x for x in out.split("\n") if x.startswith("  ")]
    assert len(snippets) == 20 and all(len(s) <= 40 and "复利" in s for s in snippets)
    assert len(out) <= MAX_TEXT


# ---- /read ----

LONG_BODY = "\n\n".join(f"第 {i} 段。" + "这是一段很长的正文内容，" * 40 for i in range(1, 16))


async def test_read_sends_the_full_text_in_order_without_markup(tmp_path):
    app, ch, providers = make_app(tmp_path)
    body = ("开头一段。\n\n![](https://x.com/a.png)\n\n### 图片 1（a.png）\n\n图里的字\n\n"
            "附件：attachments/20261015-b305ad/a.png\n附件：attachments/20261015-b305ad/b.pdf\n\n结尾一段。")
    await seed(app, "20261015-b305ad", "Acme宣布全面迁移Rust", body, summary="这是摘要。",
               points=["要点一", "要点二"], url="https://a.com/x", notes="我的想法")
    before = model_calls(app, providers)
    await app.handle(m("/read b305ad"))
    assert texts(ch) == ["《Acme宣布全面迁移Rust》\nid：20261015-b305ad\n保存于：2026-10-15\n链接：https://a.com/x\n\n"
                         "摘要：\n这是摘要。\n\n要点：\n· 要点一\n· 要点二\n\n我的备注：\n我的想法\n\n"
                         "正文：\n开头一段。\n\n（图）\n\n图片 1（a.png）\n\n图里的字\n\n结尾一段。"]
    assert "---" not in texts(ch)[0] and "attachments/" not in texts(ch)[0]
    assert app.focus.get("tg", "me") == "20261015-b305ad"  # 这篇 / 删掉上面这条 now mean it
    assert model_calls(app, providers) == before


async def test_read_ids_focus_usage_and_missing(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/read"))
    assert texts(ch)[-1] == READ_USAGE == ("用法：/read 记录号（只打最后几位也行），例：/read b305ad；"
                                             "后面可加段号（从第几段看）、“文件”（整篇发成文件）或“全部”（一次发完）")
    await seed(app, "20261015-aaa111", "甲", "甲的正文")
    await seed(app, "20261014-bbb111", "乙", "乙的正文")
    await app.handle(m("/read 111"))  # several end that way: the ambiguous list, nothing else
    assert texts(ch)[-1] == "「111」对应好几条记录，请多打几位：\n20261015-aaa111  甲\n20261014-bbb111  乙"
    await app.handle(m("/read 9999"))
    assert texts(ch)[-1] == "找不到记录：9999"
    await app.handle(m("/read 20261015-AAA111"))  # a full id, any case
    assert texts(ch)[-1].startswith("《甲》\nid：20261015-aaa111")
    app.focus.set("tg", "me", "20261014-bbb111")
    await app.handle(m("/read"))  # the current entry
    assert texts(ch)[-1].startswith("《乙》")
    app.library.delete("20261014-bbb111")
    await app.handle(m("/read"))  # the current entry is gone: as if there were none
    assert texts(ch)[-1] == READ_USAGE


async def test_read_file_gone_and_no_body_yet(tmp_path, monkeypatch):
    app, ch, _ = make_app(tmp_path)
    await seed(app, "20261015-aaa111", "https://mp.weixin.qq.com/s/x", "", url="https://mp.weixin.qq.com/s/x",
               status="needs_manual")
    await app.handle(m("/read aaa111"))
    assert texts(ch)[-1] == ("《https://mp.weixin.qq.com/s/x》（待补充）\nid：20261015-aaa111\n保存于：2026-10-15\n"
                             "链接：https://mp.weixin.qq.com/s/x\n\n正文还没读到。")
    monkeypatch.setattr(app.library, "get", lambda eid: None)  # vanished between resolving and reading
    await app.handle(m("/read aaa111"))
    assert texts(ch)[-1] == "这条记录的文件找不到了。"


async def test_read_unreadable_file_does_not_crash(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await seed(app, "20261015-aaa111", "甲", "正文")
    app.library.find("20261015-aaa111").write_text("---\nid: [\n---\n", encoding="utf-8")
    await app.handle(m("/read aaa111"))
    assert texts(ch)[-1] == "这一条的文件读不出来（可能被手动改坏了）：20261015-aaa111"


def test_read_parts_split_at_paragraphs_and_fit_every_channel():
    e = Entry(id="20261015-b305ad", title="长文", source="web", saved_at=datetime(2026, 10, 15, tzinfo=CST),
              channel="tg", body=LONG_BODY + "\n\n" + "没有标点的超长段落" * 400)
    parts = read_parts(e)
    assert len(parts) > 3 and all(len(p) <= READ_PART_CHARS for p in parts)
    assert parts[0].startswith("《长文》")
    assert all(p.strip() for p in parts)
    assert "".join(parts).count("第 7 段。") == 1
    joined = "".join(parts)
    assert joined.count("没有标点的超长段落") == 400  # a huge paragraph is cut hard, nothing lost
    short = read_parts(Entry(id="20261015-b305ad", title="短", source="manual", channel="tg", body="一句话",
                             saved_at=datetime(2026, 10, 15, tzinfo=CST)))
    assert len(short) == 1


async def test_read_one_part_entry_has_no_marker(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await seed(app, "20261015-aaa111", "甲", "正文")
    await app.handle(m("/read aaa111"))
    assert texts(ch) == ["《甲》\nid：20261015-aaa111\n保存于：2026-10-15\n\n正文：\n正文"]
    assert app.pauses == []


async def test_read_long_entry_on_telegram_sends_every_part_with_markers(tmp_path):
    app, ch, _ = make_app(tmp_path)
    e = await seed(app, "20261015-b305ad", "长文", LONG_BODY)
    n = len(read_parts(e))
    await app.handle(m("/read b305ad"))
    sent = texts(ch)
    assert len(sent) == n > 3
    assert sent[0].startswith(f"（1/{n}）\n《长文》")
    assert [s.split("\n", 1)[0] for s in sent] == [f"（{k}/{n}）" for k in range(1, n + 1)]
    assert all("接着看" not in s for s in sent)
    assert app.pauses == [PART_PAUSE_SECONDS] * (n - 1)  # paced between parts, not after the last
    await app.handle(m("/read b305ad 3"))  # from part 3 on
    assert texts(ch)[n].startswith(f"（3/{n}）\n") and len(texts(ch)) == n + n - 2
    await app.handle(m(f"/read b305ad {n + 1}"))
    assert texts(ch)[-1] == f"这条一共 {n} 段。"
    await app.handle(m("/read b305ad ０"))
    assert texts(ch)[-1] == READ_USAGE


def clawbot_app(tmp_path):
    (tmp_path / "cb").mkdir()
    ch = make_clawbot(tmp_path / "cb")
    app, _, providers = make_app(tmp_path, channel=ch)
    app.cfg.channels["clawbot"] = ChannelConfig(enabled=True, allowed_users=[OWNER])
    return app, ch


async def wechat(app, ch, text):
    incoming = await ch.to_incoming(text_msg(text))
    await app.handle(incoming)


async def test_read_on_clawbot_sends_what_the_budget_allows_and_says_how_to_go_on(tmp_path):
    app, ch = clawbot_app(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    e = await seed(app, "20261015-b305ad", "长文", LONG_BODY)
    n = len(read_parts(e))
    assert n > SEND_BUDGET + 1
    await wechat(app, ch, "/read b305ad")
    sent = [t for _, t, _ in ch._client.sent]
    assert len(sent) == SEND_BUDGET and all(len(s) <= MAX_TEXT for s in sent)
    assert sent[-1].endswith(f"后面还有 {n - SEND_BUDGET} 段，回“/read 20261015-b305ad {SEND_BUDGET + 1}”接着看。")
    assert all("接着看" not in s for s in sent[:-1])
    assert app.outbox.count() == 0  # nothing cut off, nothing held
    ch.clock.now += 481  # a new window
    await wechat(app, ch, f"/read 20261015-b305ad {SEND_BUDGET + 1}")
    sent = [t for _, t, _ in ch._client.sent][SEND_BUDGET:]
    assert sent[0].startswith(f"（{SEND_BUDGET + 1}/{n}）\n") and len(sent) == min(SEND_BUDGET, n - SEND_BUDGET)


async def test_read_on_clawbot_with_part_of_the_budget_used(tmp_path):
    app, ch = clawbot_app(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    e = await seed(app, "20261015-b305ad", "长文", LONG_BODY)
    n = len(read_parts(e))
    await wechat(app, ch, "/status")  # one of the three sends used
    await wechat(app, ch, "/read b305ad")
    sent = [t for _, t, _ in ch._client.sent][1:]
    assert len(sent) == SEND_BUDGET - 1
    assert sent[-1].endswith(f"后面还有 {n - 2} 段，回“/read 20261015-b305ad 3”接着看。")


async def test_read_on_clawbot_with_no_budget_left_holds_one_part_with_the_note(tmp_path):
    app, ch = clawbot_app(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    e = await seed(app, "20261015-b305ad", "长文", LONG_BODY)
    n = len(read_parts(e))
    for _ in range(SEND_BUDGET):
        await wechat(app, ch, "/status")
    await wechat(app, ch, "/read b305ad")
    held = [t for _, t, _ in app.outbox.queued("clawbot", OWNER)]
    assert len(held) == 1 and held[0].startswith(f"（1/{n}）\n《长文》")
    assert held[0].endswith(f"后面还有 {n - 1} 段，回“/read 20261015-b305ad 2”接着看。")


async def test_read_part_that_fails_is_held_with_the_continue_note(tmp_path):
    app, ch, _ = make_app(tmp_path)
    e = await seed(app, "20261015-b305ad", "长文", LONG_BODY)
    n = len(read_parts(e))
    real = ch.send
    count = [0]

    async def send(user_id, reply):
        count[0] += 1
        return SendResult(False) if count[0] == 2 else await real(user_id, reply)

    ch.send = send
    await app.handle(m("/read b305ad"))
    assert len(ch.sent) == 1  # stopped after the failed part: the order is kept
    held = [t for _, t, _ in app.outbox.queued("tg", "me")]
    assert held[0].startswith(f"（2/{n}）")
    assert held[-1] == f"后面还有 {n - 2} 段，回“/read 20261015-b305ad 3”接着看。"


async def test_read_and_find_end_an_open_image_window(tmp_path):
    from octopus.pending import NEW
    app, ch, _ = make_app(tmp_path)
    for text in ("/read", "/find 复利"):
        app.pending.set("tg", "me", "20261015-aaa111", origin=NEW)
        await app.handle(m(text))
        assert app.pending.active("tg", "me") is None


def test_clawbot_sends_left(tmp_path):
    ch = make_clawbot(tmp_path)
    assert ch.sends_left(OWNER) == SEND_BUDGET
    ch._sent[OWNER] = __import__("collections").deque([0.0] * (SEND_BUDGET + 1))
    assert ch.sends_left(OWNER) == 0


# ---- help and menus ----

def test_help_and_menu_list_read_and_find():
    assert "/read 记录号 — 在聊天里看这条的全文（长的一次发几段，点【继续】接着看）" in HELP
    assert "/find 关键词 — 按关键词找记录（不经过 AI，几个词用空格隔开）" in HELP
    names = [c for c, _ in COMMAND_MENU]
    assert {"read", "find"} <= set(names)
    for name in names:
        assert f"/{name}" in HELP


async def test_read_on_a_channel_without_a_budget(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FakeChannel("tg"))
    await seed(app, "20261015-aaa111", "甲", "正文")
    await app.handle(m("/read aaa111"))
    assert texts(ch)[-1].startswith("《甲》")


async def test_after_read_this_one_means_the_entry_read(tmp_path):
    app, ch, providers = make_app(tmp_path)
    await seed(app, "20261015-aaa111", "甲", "甲的正文")
    await seed(app, "20261015-bbb222", "乙", "乙的正文")
    app.focus.set("tg", "me", "20261015-bbb222")  # e.g. 乙 was saved last
    await app.handle(m("/read aaa111"))
    await app.handle(m("这篇讲了什么"))
    prompt = providers["claude:sonnet"].calls[-1][0]
    assert f"[1] 《甲》 id：20261015-aaa111\n{PINNED_MARK}" in prompt and f"《乙》 id：20261015-bbb222\n{PINNED_MARK}" not in prompt


async def test_read_with_no_id_on_a_quote_reply_reads_the_quoted_prompts_entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await seed(app, "20261015-aaa111", "甲", "甲的正文")
    await seed(app, "20261015-bbb222", "乙", "乙的正文")
    app.focus.set("tg", "me", "20261015-bbb222")
    app.pending.set("tg", "me", "20261015-aaa111", "m7")  # the "please send screenshots" prompt for 甲
    quoted = m("/read")
    quoted.reply_to_id = "m7"
    await app.handle(quoted)
    assert texts(ch)[-1].startswith("《甲》")
    other = m("/read")
    other.reply_to_id = "m1"  # any other quoted message: no entry, as for "这篇"
    await app.handle(other)
    assert texts(ch)[-1] == READ_USAGE


async def test_read_worst_case_message_fits_clawbot(tmp_path):
    """122 parts of exactly READ_PART_CHARS: a 3-digit marker and the continue note on a full part."""
    app, ch = clawbot_app(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    e = await seed(app, "20261015-b305ad", "长文", ("字" * READ_PART_CHARS + "\n\n") * 121)
    n = len(read_parts(e))
    assert n >= 100 and max(map(len, read_parts(e))) == READ_PART_CHARS
    await wechat(app, ch, f"/read b305ad {n - 20}")
    sent = [t for _, t, _ in ch._client.sent]
    assert len(sent) == SEND_BUDGET and sent[-1].endswith("接着看。")
    assert max(map(len, sent)) == len(sent[-1]) <= MAX_TEXT


async def test_read_part_out_of_range_on_clawbot(tmp_path):
    app, ch = clawbot_app(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    e = await seed(app, "20261015-b305ad", "长文", LONG_BODY)
    n = len(read_parts(e))
    await wechat(app, ch, f"/read b305ad {n + 1}")
    assert [t for _, t, _ in ch._client.sent] == [f"这条一共 {n} 段。"]


async def test_find_folds_full_width_text_and_query_alike(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await seed(app, "20261013-aaa111", "烘焙配方（第二版）", "家庭，烘焙是关键。全角ＡＩ工具")
    await seed(app, "20261014-bbb222", "半角", "用 AI 写作")
    for q, ids in [("烘焙配方（第二版）", ["aaa111"]), ("烘焙配方(第二版)", ["aaa111"]), ("家庭，烘焙", ["aaa111"]),
                   ("家庭,烘焙", ["aaa111"]), ("ＡＩ", ["bbb222", "aaa111"]), ("ai", ["bbb222", "aaa111"])]:
        await app.handle(m(f"/find {q}"))
        out = texts(ch)[-1]
        assert [x[9:15] for x in out.split("\n") if x.startswith("2026")] == ids, (q, out)
    await app.handle(m("/find ai"))  # the snippet shows the text as saved
    assert "  家庭，烘焙是关键。全角ＡＩ工具" in texts(ch)[-1]


async def test_find_title_only_match_of_an_entry_without_text_has_no_snippet(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await seed(app, "20261013-aaa111", "复利的力量", "", status="needs_manual")
    await app.handle(m("/find 复利"))
    assert texts(ch)[-1] == "含「复利」的记录（1 条）：\n20261013-aaa111  复利的力量（待补充）\n看全文：/read 记录号"


async def test_find_ignores_attachment_paths_and_image_markup(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await seed(app, "20261013-aaa111", "截图", "![](https://x.com/a.png)\n\n### 图片 1（a.png）\n\n复利的图\n\n"
                                            "附件：attachments/20261013-aaa111/a.png")
    for q in ("attachments", "https", "###"):
        await app.handle(m(f"/find {q}"))
        assert texts(ch)[-1] == FIND_WORDS_NONE.format(q=q)
    await app.handle(m("/find 复利"))
    assert "\n  （图） 图片 1（a.png） 复利的图\n" in texts(ch)[-1]


def test_read_image_url_with_parentheses_leaves_nothing_behind():
    e = Entry(id="20261015-b305ad", title="图", source="web", saved_at=datetime(2026, 10, 15, tzinfo=CST),
              channel="tg", body="前文 ![x](https://x.com/a_(1).png) 后文")
    assert read_parts(e)[0].endswith("正文：\n前文 （图） 后文")
