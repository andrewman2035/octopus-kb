import asyncio
from pathlib import Path
from types import SimpleNamespace

from octopus.channels.base import Button, Reply, split_text
from octopus.channels.telegram import MAX_TEXT, TOO_BIG, TelegramChannel


class FakeFile:
    def __init__(self, data):
        self.data = data

    async def download_to_drive(self, custom_path):
        open(custom_path, "wb").write(self.data)
        return custom_path


class FakeBot:
    def __init__(self):
        self.sent, self.photos, self.n = [], [], 100

    async def get_file(self, obj):
        return FakeFile(b"filedata")

    async def send_message(self, chat_id, text, reply_markup=None):
        self.n += 1
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=self.n)

    async def send_photo(self, chat_id, photo, caption=""):
        self.photos.append((chat_id, caption))
        return SimpleNamespace(message_id=999)


def msg(**kw):
    base = dict(text=None, caption=None, photo=(), voice=None, audio=None, video=None, video_note=None,
                document=None, reply_to_message=None, message_id=5,
                from_user=SimpleNamespace(id=42), chat=SimpleNamespace(id=42, type="private"))
    base.update(kw)
    return SimpleNamespace(**base)


def test_split_text_prefers_newlines():
    parts = split_text("a" * 10 + "\n" + "b" * 10, 15)
    assert parts == ["a" * 10, "b" * 10]
    assert split_text("x" * 30, 10) == ["x" * 10] * 3
    assert split_text("", 10) == [""]


async def test_text_message(tmp_path):
    ch = TelegramChannel("t", tmp_path, bot=FakeBot())
    m = await ch.to_incoming(msg(text="你好", reply_to_message=SimpleNamespace(message_id=3)))
    assert (m.channel, m.user_id, m.text, m.reply_to_id, m.message_id) == ("telegram", "42", "你好", "3", "5")


async def test_photo_downloads_largest(tmp_path):
    ch = TelegramChannel("t", tmp_path, bot=FakeBot())
    small, big = SimpleNamespace(file_id="s", file_size=10), SimpleNamespace(file_id="b", file_size=1000)
    m = await ch.to_incoming(msg(photo=(small, big), caption="看图"))
    assert m.text == "看图"
    [a] = m.attachments
    assert a.kind == "image" and a.path.read_bytes() == b"filedata" and a.path.suffix == ".jpg"
    assert a.path.parent.parent == tmp_path


async def test_voice_and_document(tmp_path):
    ch = TelegramChannel("t", tmp_path, bot=FakeBot())
    v = await ch.to_incoming(msg(voice=SimpleNamespace(file_id="v", file_size=10, mime_type="audio/ogg")))
    assert v.attachments[0].kind == "audio" and v.attachments[0].path.suffix == ".ogg"
    assert v.attachments[0].voice is True  # hold-to-talk: handled as spoken words
    d = await ch.to_incoming(msg(document=SimpleNamespace(file_id="d", file_size=10, file_name="论文.pdf",
                                                          mime_type="application/pdf")))
    assert d.attachments[0].kind == "file" and d.attachments[0].filename == "论文.pdf"
    assert d.attachments[0].voice is False


async def test_audio_file_and_video_note_are_not_voice(tmp_path):
    ch = TelegramChannel("t", tmp_path, bot=FakeBot())
    a = await ch.to_incoming(msg(audio=SimpleNamespace(file_id="a", file_size=10, file_name="talk.mp3",
                                                       mime_type="audio/mpeg")))
    assert a.attachments[0].kind == "audio" and a.attachments[0].voice is False
    n = await ch.to_incoming(msg(video_note=SimpleNamespace(file_id="n", file_size=10, mime_type="video/mp4")))
    assert n.attachments[0].kind == "video" and n.attachments[0].voice is False


async def test_too_big_file_gets_hint(tmp_path):
    bot = FakeBot()
    ch = TelegramChannel("t", tmp_path, bot=bot)
    m = await ch.to_incoming(msg(video=SimpleNamespace(file_id="v", file_size=30_000_000)))
    assert m is None
    assert bot.sent[-1][1] == TOO_BIG


async def test_group_messages_ignored(tmp_path):
    ch = TelegramChannel("t", tmp_path, bot=FakeBot())
    assert await ch.to_incoming(msg(text="hi", chat=SimpleNamespace(id=-1, type="group"))) is None


async def test_send_splits_and_puts_buttons_on_last_chunk(tmp_path):
    bot = FakeBot()
    ch = TelegramChannel("t", tmp_path, bot=bot)
    r = await ch.send("42", Reply("x" * (MAX_TEXT + 10), [Button("改成提问", "fix:1", "回复“问”改成提问")]))
    assert r.ok and r.message_id == "102"
    assert len(bot.sent) == 2 and bot.sent[0][2] is None
    keyboard = bot.sent[1][2].inline_keyboard
    assert keyboard[0][0].text == "改成提问" and keyboard[0][0].callback_data == "fix:1"


async def test_send_failure_is_reported(tmp_path):
    class Broken(FakeBot):
        async def send_message(self, *a, **k):
            raise RuntimeError("network")

    assert (await TelegramChannel("t", tmp_path, bot=Broken()).send("42", Reply("x"))).ok is False


async def test_callback_becomes_incoming(tmp_path):
    answered, edited = [], []

    async def answer():
        answered.append(True)

    async def edit(reply_markup=None):
        edited.append(reply_markup)

    q = SimpleNamespace(data="fix:7", from_user=SimpleNamespace(id=42), answer=answer,
                        edit_message_reply_markup=edit, message=SimpleNamespace(message_id=9))
    m = await TelegramChannel("t", tmp_path, bot=FakeBot()).on_callback(q)
    assert (m.callback, m.user_id, m.text) == ("fix:7", "42", "")
    assert answered == [True] and edited == [None]


async def test_send_image(tmp_path):
    bot = FakeBot()
    png = tmp_path / "qr.png"
    png.write_bytes(b"png")
    r = await TelegramChannel("t", tmp_path, bot=bot).send_image("42", png, "请扫码")
    assert r.ok and bot.photos == [(42, "请扫码")]


async def test_partial_send_failure_reports_ok_with_last_id(tmp_path):
    class FlakyBot(FakeBot):
        async def send_message(self, chat_id, text, reply_markup=None):
            if len(self.sent) >= 1:
                raise RuntimeError("network")
            return await super().send_message(chat_id, text, reply_markup)

    bot = FlakyBot()
    r = await TelegramChannel("t", tmp_path, bot=bot).send("42", Reply("x" * (MAX_TEXT * 2 + 10)))
    assert r.ok is True and r.message_id == "101"
    assert len(bot.sent) == 1


async def test_hostile_filename_stays_inside_incoming_dir(tmp_path):
    ch = TelegramChannel("t", tmp_path, bot=FakeBot())
    for name in ("..", "../../evil.pdf", ""):
        m = await ch.to_incoming(msg(document=SimpleNamespace(file_id="d", file_size=10, file_name=name,
                                                              mime_type="application/pdf")))
        assert m.attachments[0].path.parent.parent == tmp_path


# ---- fix round 1 ----

import logging

import pytest

TOKEN = "123456:SECRET-token-value"


async def _run_start(monkeypatch, exc):
    from telegram.ext import ExtBot

    async def boom(self):
        raise exc

    monkeypatch.setattr(ExtBot, "initialize", boom)

    async def on_message(m):
        pass

    return await TelegramChannel(TOKEN, Path("/nonexistent")).start(on_message)


async def test_invalid_token_error_does_not_leak_token(monkeypatch, caplog):
    from telegram.error import InvalidToken

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError) as ei:
            await _run_start(monkeypatch, InvalidToken(f"The token `{TOKEN}` was rejected by the server."))
    assert "telegram_token" in str(ei.value) and TOKEN not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert TOKEN not in caplog.text


async def test_any_error_containing_token_is_scrubbed(monkeypatch):
    with pytest.raises(RuntimeError) as ei:
        await _run_start(monkeypatch, OSError(f"https://api.telegram.org/bot{TOKEN}/getMe failed"))
    assert TOKEN not in str(ei.value) and ei.value.__suppress_context__


async def test_other_errors_propagate_unchanged(monkeypatch):
    with pytest.raises(ValueError, match="boom"):
        await _run_start(monkeypatch, ValueError("boom"))


class BadDownloadBot(FakeBot):
    def __init__(self, fail_on, message="Timed out"):
        super().__init__()
        self.fail_on, self.message, self.calls = fail_on, message, 0

    async def get_file(self, obj):
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError(self.message)
        return await super().get_file(obj)


def two_files(**kw):
    return msg(photo=(SimpleNamespace(file_id="p", file_size=10),),
               document=SimpleNamespace(file_id="d", file_size=10, file_name="a.pdf", mime_type="application/pdf"), **kw)


async def test_download_failure_cleans_staging_and_tells_user(tmp_path):
    bot = BadDownloadBot(fail_on=2)
    ch = TelegramChannel("t", tmp_path, bot=bot)
    assert await ch.to_incoming(two_files(text="hi")) is None
    assert list(tmp_path.iterdir()) == []  # the photo downloaded before the failure is gone too
    assert bot.sent[-1][:2] == (42, "附件下载失败，请重发一次。")


async def test_download_failure_too_big_error_gets_size_hint(tmp_path):
    bot = BadDownloadBot(fail_on=1, message="Bad Request: File is too big")
    ch = TelegramChannel("t", tmp_path, bot=bot)
    assert await ch.to_incoming(two_files()) is None
    assert bot.sent[-1][1] == TOO_BIG and list(tmp_path.iterdir()) == []


async def test_too_big_after_a_download_cleans_earlier_files(tmp_path):
    bot = FakeBot()
    ch = TelegramChannel("t", tmp_path, bot=bot)
    m = msg(photo=(SimpleNamespace(file_id="p", file_size=10),), video=SimpleNamespace(file_id="v", file_size=30_000_000))
    assert await ch.to_incoming(m) is None
    assert list(tmp_path.iterdir()) == [] and bot.sent[-1][1] == TOO_BIG


async def test_disallowed_user_gets_nothing_downloaded_or_sent(tmp_path, caplog):
    bot = BadDownloadBot(fail_on=0)
    ch = TelegramChannel("t", tmp_path, bot=bot, allowed=lambda uid: uid == "7")
    with caplog.at_level(logging.INFO):
        assert await ch.to_incoming(two_files(text="hi")) is None
        big = msg(video=SimpleNamespace(file_id="v", file_size=30_000_000))
        assert await ch.to_incoming(big) is None
    assert bot.calls == 0 and bot.sent == [] and list(tmp_path.iterdir()) == []
    assert "42" in caplog.text
    answered = []

    async def answer():
        answered.append(1)

    q = SimpleNamespace(data="fix:7", from_user=SimpleNamespace(id=42), answer=answer,
                        edit_message_reply_markup=answer, message=SimpleNamespace(message_id=9))
    assert await ch.on_callback(q) is None and answered == []


async def test_allowed_user_passes(tmp_path):
    ch = TelegramChannel("t", tmp_path, bot=FakeBot(), allowed=lambda uid: uid == "42")
    assert (await ch.to_incoming(msg(text="hi"))).text == "hi"


async def test_callback_answer_failure_is_harmless(tmp_path):
    async def bad():
        raise RuntimeError("query too old")

    q = SimpleNamespace(data="fix:7", from_user=SimpleNamespace(id=42), answer=bad,
                        edit_message_reply_markup=bad, message=SimpleNamespace(message_id=9))
    m = await TelegramChannel("t", tmp_path, bot=FakeBot()).on_callback(q)
    assert m.callback == "fix:7"


async def test_handlers_never_raise(tmp_path):
    got = []

    async def on_message(m):
        got.append(m)

    class Exploding(FakeBot):
        async def get_file(self, obj):
            raise RuntimeError("x")

        async def send_message(self, *a, **k):
            raise RuntimeError("y")

    ch = TelegramChannel("t", tmp_path, bot=Exploding())
    await ch.handle_message(SimpleNamespace(effective_message=two_files()), on_message)
    q = SimpleNamespace(data="d", from_user=None, answer=None, edit_message_reply_markup=None)
    await ch.handle_callback(SimpleNamespace(callback_query=q), on_message)  # q.answer is None: raises inside
    assert got == []
    good = TelegramChannel("t", tmp_path, bot=FakeBot())
    await good.handle_message(SimpleNamespace(effective_message=msg(text="hi")), on_message)
    await ch.drain()
    await good.drain()
    assert [m.text for m in got] == ["hi"]


def test_split_text_never_emits_empty_chunks():
    assert split_text("\n" * 10 + "x" * 5, 10) == ["x" * 5]
    assert "" not in split_text("a\n\n\n\n\n\n\n\n\n\n\nb", 5)
    assert split_text("\n" * 20, 10) == [""]


async def test_extensionless_names_get_suffix_from_mime(tmp_path):
    ch = TelegramChannel("t", tmp_path, bot=FakeBot())
    a = await ch.to_incoming(msg(audio=SimpleNamespace(file_id="a", file_size=10, file_name=None, mime_type="audio/mpeg")))
    assert a.attachments[0].path.suffix == ".mp3"
    d = await ch.to_incoming(msg(document=SimpleNamespace(file_id="d", file_size=10, file_name="readme",
                                                          mime_type="application/pdf")))
    assert d.attachments[0].filename == "readme.pdf"
    u = await ch.to_incoming(msg(document=SimpleNamespace(file_id="d", file_size=10, file_name=None, mime_type=None)))
    assert u.attachments[0].filename == "file"


# ---- final review M1 ----

async def test_filenames_use_shared_safe_filename(tmp_path):
    ch = TelegramChannel("t", tmp_path, bot=FakeBot())
    m = await ch.to_incoming(msg(document=SimpleNamespace(file_id="d", file_size=10, file_name="a\x00b\n" + "长" * 100 + ".pdf",
                                                          mime_type="application/pdf")))
    name = m.attachments[0].filename
    assert len(name.encode("utf-8")) <= 200 and name.startswith("ab长") and name.endswith(".pdf")
    m = await ch.to_incoming(msg(document=SimpleNamespace(file_id="d", file_size=10, file_name="..\\..\\w.pdf",
                                                          mime_type="application/pdf")))
    assert m.attachments[0].filename == "w.pdf"


# ---- Task 5: command menu, button rows ----

async def _start_until_polling(monkeypatch, set_commands):
    """Runs start() through initialize/start with no network; start_polling ends it with a sentinel."""
    from telegram import User
    from telegram.ext import ExtBot, Updater

    async def initialize(self):
        self._bot_user = User(1, "octopus", True, username="octopus_bot")

    async def noop(self):
        pass

    async def stop_polling(self, *a, **k):
        raise ValueError("polling reached")

    monkeypatch.setattr(ExtBot, "initialize", initialize)
    monkeypatch.setattr(ExtBot, "shutdown", noop)
    monkeypatch.setattr(ExtBot, "set_my_commands", set_commands)
    monkeypatch.setattr(Updater, "start_polling", stop_polling)

    async def on_message(m):
        pass

    with pytest.raises(ValueError, match="polling reached"):
        await TelegramChannel(TOKEN, Path("/nonexistent")).start(on_message)


async def test_start_sets_the_command_menu(monkeypatch):
    seen = []

    async def set_commands(self, commands, *a, **k):
        seen.append([(c.command, c.description) for c in commands])
        return True

    await _start_until_polling(monkeypatch, set_commands)
    assert seen == [[("s", "存成笔记（/s 后面跟内容）"), ("recent", "最近存的 10 条（/recent 30 看 30 条）"),
                     ("list", "今天存的（/list 2026-10-14 看那一天）"), ("read", "看一条的全文（/read 记录号）"),
                     ("find", "按关键词找记录（/find 关键词）"), ("delete", "删除（可以一次写多个 id）"),
                     ("resummarize", "重新生成摘要"), ("done", "结束 /add、/new 的补充"), ("model", "查看/切换模型"),
                     ("status", "运行状态"),
                     ("clawbot", "微信重新登录二维码"), ("help", "使用说明")]]


async def test_command_menu_failure_is_not_fatal(monkeypatch, caplog):
    async def set_commands(self, commands, *a, **k):
        raise RuntimeError("menu down")

    with caplog.at_level(logging.WARNING):
        await _start_until_polling(monkeypatch, set_commands)  # still reaches polling
    assert "command menu" in caplog.text


async def test_many_buttons_are_laid_out_in_rows(tmp_path):
    bot = FakeBot()
    ch = TelegramChannel("t", tmp_path, bot=bot)
    labels = ["最近存的", "运行状态", "当前模型", "切到本机模型", "恢复默认模型", "删除一条"]
    await ch.send("42", Reply("guide", [Button(x, f"cmd:{i}", "") for i, x in enumerate(labels)]))
    rows = [[b.text for b in row] for row in bot.sent[-1][2].inline_keyboard]
    assert rows == [labels[:3], labels[3:5], labels[5:]]
    picker = [Button(f"删除 {'很长的标题' * 3}"[:15], f"del:{i}", "") for i in range(10)]
    await ch.send("42", Reply("pick", picker))
    rows = bot.sent[-1][2].inline_keyboard
    assert len(rows) == 10 and all(len(r) == 1 for r in rows)  # Telegram allows at most 8 per row
    await ch.send("42", Reply("ok?", [Button("确认", "op:1:yes", ""), Button("取消", "op:1:no", "")]))
    assert [len(r) for r in bot.sent[-1][2].inline_keyboard] == [2]


async def test_command_buttons_stay_after_a_tap(tmp_path):
    edited = []

    async def answer():
        pass

    async def edit(reply_markup=None):
        edited.append(reply_markup)

    ch = TelegramChannel("t", tmp_path, bot=FakeBot())
    for data in ("cmd:recent", "del:20261013-73b48c", "op:1:yes", "read:20261015-b305ad:6",
                 "read:20261015-b305ad:file"):
        q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=42), answer=answer,
                            edit_message_reply_markup=edit, message=SimpleNamespace(message_id=9))
        assert (await ch.on_callback(q)).callback == data
    # the guide and /read's continue / file buttons stay (P35); a delete or confirm button is used up
    assert edited == [None, None]


# ---- P8: one user's messages reach App in the order they were sent ----

class SlowBot(FakeBot):
    """Downloads wait until released: a photo still downloading while the next message comes in."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def get_file(self, obj):
        await self.release.wait()
        return FakeFile(b"filedata")


def photo(user=42, mid=5):
    return msg(photo=(SimpleNamespace(file_id="p", file_size=10),), message_id=mid, from_user=SimpleNamespace(id=user))


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def test_a_text_after_a_slow_photo_is_handed_over_after_it(tmp_path):
    got = []

    async def on_message(m):
        got.append((m.text, len(m.attachments)))

    bot = SlowBot()
    ch = TelegramChannel("t", tmp_path, bot=bot)
    await asyncio.wait_for(ch.handle_message(SimpleNamespace(effective_message=photo(mid=1)), on_message), 1)
    await ch.handle_message(SimpleNamespace(effective_message=photo(mid=2)), on_message)
    await ch.handle_message(SimpleNamespace(effective_message=msg(text="上面这些图存成一篇", message_id=3)), on_message)
    await _settle()
    assert got == []  # read at once, but not routed before the photos sent earlier
    bot.release.set()
    await ch.drain()
    assert got == [("", 1), ("", 1), ("上面这些图存成一篇", 0)]


async def test_other_users_and_taps_do_not_wait_for_a_download(tmp_path):
    got = []

    async def on_message(m):
        got.append((m.user_id, m.text or m.callback))

    bot = SlowBot()
    ch = TelegramChannel("t", tmp_path, bot=bot)
    await ch.handle_message(SimpleNamespace(effective_message=photo()), on_message)
    await ch.handle_message(SimpleNamespace(effective_message=msg(text="hi", from_user=SimpleNamespace(id=43))),
                            on_message)

    async def ok():
        return None

    q = SimpleNamespace(data="op:1:yes", from_user=SimpleNamespace(id=42), answer=ok, edit_message_reply_markup=None)
    await ch.handle_callback(SimpleNamespace(callback_query=q), on_message)
    await _settle()
    assert sorted(got) == [("42", "op:1:yes"), ("43", "hi")]
    bot.release.set()
    await ch.drain()
    assert got[-1] == ("42", None)  # the photo, after the tap
    assert ch._last == {}  # nothing kept once a user's messages are all handed over


class PerFileBot(FakeBot):
    """Each file waits for its own release; "boom" fails once released."""

    def __init__(self):
        super().__init__()
        self.gates = {}

    async def get_file(self, obj):
        await self.gates.setdefault(obj.file_id, asyncio.Event()).wait()
        if obj.file_id == "boom":
            raise RuntimeError("proxy timeout")
        return FakeFile(b"x")


def photo_of(fid, mid):
    return msg(photo=(SimpleNamespace(file_id=fid, file_size=10),), message_id=mid)


async def test_order_holds_when_later_downloads_finish_first_and_one_fails(tmp_path):
    got = []

    async def on_message(m):
        got.append(m.message_id)

    bot = PerFileBot()
    ch = TelegramChannel("t", tmp_path, bot=bot)
    for fid, mid in (("a", 1), ("boom", 2), ("c", 3)):
        await ch.handle_message(SimpleNamespace(effective_message=photo_of(fid, mid)), on_message)
    await ch.handle_message(SimpleNamespace(effective_message=msg(text="t", message_id=4)), on_message)
    await _settle()
    for fid in ("c", "boom"):
        bot.gates.setdefault(fid, asyncio.Event()).set()
        await _settle()
    assert got == []  # all waiting for the first photo
    bot.gates["a"].set()
    await ch.drain()
    assert got == ["1", "3", "4"] and ch._last == {}
    assert bot.sent[-1][1] == "附件下载失败，请重发一次。"


async def test_a_stuck_download_gives_up_and_lets_the_next_message_through(tmp_path, monkeypatch):
    import octopus.channels.telegram as tg

    monkeypatch.setattr(tg, "READ_TIMEOUT_SECONDS", 0.05)
    got = []

    async def on_message(m):
        got.append(m.message_id)

    bot = PerFileBot()  # "stuck" is never released
    ch = TelegramChannel("t", tmp_path, bot=bot)
    await ch.handle_message(SimpleNamespace(effective_message=photo_of("stuck", 1)), on_message)
    await ch.handle_message(SimpleNamespace(effective_message=msg(text="t", message_id=2)), on_message)
    await asyncio.wait_for(ch.drain(), 2)
    assert got == ["2"] and bot.sent[-1][1] == tg.DOWNLOAD_FAILED
    assert not any(p.is_file() for p in tmp_path.rglob("*"))  # nothing staged is left behind


async def test_messages_dropped_before_the_app_are_logged_with_their_reply(tmp_path, caplog):
    import logging

    from octopus.channels.base import user_tag

    tag = user_tag("42")
    caplog.set_level(logging.INFO)
    bot = FakeBot()
    ch = TelegramChannel("t", tmp_path, bot=bot)
    assert await ch.to_incoming(msg(video=SimpleNamespace(file_id="v", file_size=30_000_000))) is None
    assert await ch.to_incoming(msg(message_id=6)) is None  # a sticker, a location: nothing readable
    assert await BadDownloadChannel(tmp_path).to_incoming(two_files()) is None
    text = caplog.text
    assert f"in telegram {tag} not handed over: too big" in text
    assert f"in telegram {tag} not handed over: nothing readable" in text
    assert f"in telegram {tag} not handed over: download failed" in text
    assert f'out telegram {tag} {len(TOO_BIG)}字 "{TOO_BIG[:20]}' in text


def BadDownloadChannel(tmp_path):
    return TelegramChannel("t", tmp_path / "bad", bot=BadDownloadBot(fail_on=1))
