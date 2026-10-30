"""P35: /read sends a long entry a batch at a time, with "continue" and "send as a file" buttons (typed hints on
ClawBot); /read <id> K / all / file; the whole note as a file on Telegram, Discord and Feishu."""
from types import SimpleNamespace

import pytest
from fakes import FakeChannel
from test_clawbot import FakeClient, text_msg
from test_read_find import OWNER, clawbot_app, make_app, seed, texts, wechat

from octopus import i18n
from octopus.channels.base import Button, SendResult
from octopus.channels.clawbot import SEND_BUDGET
from octopus.channels.discord import MAX_CUSTOM_ID
from octopus.channels.discord import MAX_UPLOAD as DISCORD_MAX
from octopus.channels.feishu import MAX_UPLOAD as FEISHU_MAX
from octopus.channels.feishu import FeishuChannel, card_json
from octopus.channels.telegram import MAX_UPLOAD as TELEGRAM_MAX
from octopus.channels.telegram import TelegramChannel
from octopus.app import READ_TAP_REPEAT_SECONDS
from octopus.commands import HELP, READ_USAGE, read_parts
from octopus.config import ChannelConfig, ConfigError, load_config
from octopus.models import IncomingMessage

EID = "20261015-b305ad"


def body(parts: int) -> str:
    """One paragraph per part: the title block joins the first."""
    return "\n\n".join(f"第{i}段" + "字" * 1700 for i in range(1, parts + 1))


class FileChannel(FakeChannel):
    """A channel that can send files (Telegram, Discord, Feishu)."""

    def __init__(self, name="tg", reason=""):
        super().__init__(name)
        self.files: list[tuple[str, bytes, str]] = []
        self.reason = reason  # set: the file is refused for this reason

    async def send_file(self, user_id, data, filename):
        if self.reason:
            return SendResult(False, reason=self.reason)
        self.files.append((user_id, data, filename))
        return SendResult(True, "f1")


def tap(data, user="me", channel="tg"):
    return IncomingMessage(channel=channel, user_id=user, text="", callback=data)


def buttons_of(ch):
    return [bs for _, bs in ch.sent_full]


async def long_entry(app, parts=38, title="长文"):
    e = await seed(app, EID, title, body(parts))
    assert len(read_parts(e)) == parts
    return e


# ---- batches and buttons ----

async def test_a_38_part_entry_comes_five_parts_at_a_time_with_continue_and_file_buttons(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    await long_entry(app)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read b305ad"))
    sent = texts(ch)
    assert [s.split("\n", 1)[0] for s in sent] == [f"（{k}/38）" for k in range(1, 6)]
    assert buttons_of(ch)[:4] == [[]] * 4  # only the batch's last message has buttons
    cont, whole = buttons_of(ch)[4]
    assert (cont.label, cont.data) == ("继续（6/38）", f"read:{EID}:6")
    assert (whole.label, whole.data) == ("整篇发文件", f"read:{EID}:file")
    assert cont.fallback == f"后面还有 33 段，回“/read {EID} 6”接着看。"
    assert whole.fallback == f"回“/read {EID} 文件”要整篇文件。"
    assert sent[-1].endswith("\n\n这条很长，一共 38 段；想一次拿到全文，点【整篇发文件】。")  # first batch only
    assert all("很长" not in s for s in sent[:-1]) and all("接着看" not in s for s in sent)
    assert app.pauses == [1.0] * 4
    assert app.focus.get("tg", "me") == EID


async def test_continue_sends_the_next_batch_and_the_last_batch_has_no_continue(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    await long_entry(app)
    await app.handle(tap(f"read:{EID}:6"))
    assert [s.split("\n", 1)[0] for s in texts(ch)] == [f"（{k}/38）" for k in range(6, 11)]
    assert buttons_of(ch)[-1][0].label == "继续（11/38）"
    assert "很长" not in texts(ch)[-1]  # said under the first batch only
    await app.handle(tap(f"read:{EID}:36"))  # an old batch's button: its state is in the data
    assert [s.split("\n", 1)[0] for s in texts(ch)[5:]] == ["（36/38）", "（37/38）", "（38/38）"]
    assert buttons_of(ch)[5:] == [[], [], []]


async def test_an_entry_of_up_to_ten_parts_does_not_say_it_is_long(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    await long_entry(app, parts=8)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read b305ad"))
    assert len(texts(ch)) == 5 and "很长" not in texts(ch)[-1]
    assert [b.label for b in buttons_of(ch)[-1]] == ["继续（6/8）", "整篇发文件"]


async def test_read_from_part_k_and_all_at_once(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    await long_entry(app, parts=12)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read b305ad 4"))
    assert [s.split("\n", 1)[0] for s in texts(ch)] == [f"（{k}/12）" for k in range(4, 9)]
    assert "很长" not in texts(ch)[-1]
    for word in ("全部", "all", "ALL", "ａｌｌ"):
        ch.sent.clear()
        ch.sent_full.clear()
        await app.handle(IncomingMessage(channel="tg", user_id="me", text=f"/read b305ad {word}"))
        assert len(texts(ch)) == 12 and buttons_of(ch) == [[]] * 12


async def test_batch_size_comes_from_the_config(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    app.cfg.read_batch_parts = 3
    await long_entry(app)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read b305ad"))
    assert len(texts(ch)) == 3 and buttons_of(ch)[-1][0].label == "继续（4/38）"


async def test_bad_taps_are_ignored(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    await long_entry(app)
    for data in ("read:b305ad:6", f"read:{EID}:x", f"read:{EID}:", f"read:{EID}:1234567", f"read:{EID}:６",
                 f"read:{EID}"):
        await app.handle(tap(data))
    assert ch.sent == [] and ch.files == []


async def test_a_channel_without_files_offers_only_continue(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FakeChannel("tg"))
    await long_entry(app)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read b305ad"))
    assert len(texts(ch)) == 5 and [b.label for b in buttons_of(ch)[-1]] == ["继续（6/38）"]
    assert "很长" not in texts(ch)[-1]  # no file button to point at


async def test_a_part_that_fails_is_held_with_both_hints(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    await long_entry(app)
    real = ch.send
    count = [0]

    async def send(user_id, reply):
        count[0] += 1
        return SendResult(False) if count[0] == 2 else await real(user_id, reply)

    ch.send = send
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read b305ad"))
    held = [t for _, t, _ in app.outbox.queued("tg", "me")]
    assert held[0].startswith("（2/38）")
    assert held[-1] == f"后面还有 36 段，回“/read {EID} 3”接着看。\n回“/read {EID} 文件”要整篇文件。"


# ---- the whole note as a file ----

async def test_read_file_sends_the_note_as_stored(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    await long_entry(app, title="Acme 宣布：迁移 Rust?")
    stored = app.library.find(EID).read_bytes()
    for word in ("文件", "file", "FILE"):
        await app.handle(IncomingMessage(channel="tg", user_id="me", text=f"/read b305ad {word}"))
    assert ch.sent == []
    assert ch.files == [("me", stored, "Acme-宣布-迁移-Rust-b305ad.md")] * 3
    assert stored.decode("utf-8").startswith("---\n")  # frontmatter and all
    await app.handle(tap(f"read:{EID}:file"))
    assert len(ch.files) == 4 and app.focus.get("tg", "me") == EID


@pytest.mark.parametrize("reason,said", [("too-big", "整篇太大，发不了文件，改成分段发。"),
                                         ("error", "文件没发出去，改成分段发。")])
async def test_a_file_that_cannot_go_falls_back_to_batches(tmp_path, reason, said):
    app, ch, _ = make_app(tmp_path, channel=FileChannel(reason=reason))
    await long_entry(app)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read b305ad 文件"))
    sent = texts(ch)
    assert len(sent) == 5 and sent[0].startswith(f"{said}\n\n（1/38）\n《长文》")
    assert [b.label for b in buttons_of(ch)[-1]] == ["继续（6/38）"]  # the file is not offered again right away
    assert "很长" not in sent[-1]


async def test_read_file_on_a_channel_without_files_sends_batches(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FakeChannel("tg"))
    await long_entry(app)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read b305ad file"))
    assert len(texts(ch)) == 5 and texts(ch)[0].startswith("这里没法发文件，改成分段发。\n\n（1/38）")


# ---- ClawBot: its budget, typed hints, no files ----

async def test_clawbot_sends_its_budget_with_the_typed_continue_hint_and_no_file_hint(tmp_path):
    app, ch = clawbot_app(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    await long_entry(app)
    await wechat(app, ch, "/read b305ad")
    sent = [t for _, t, _ in ch._client.sent]
    assert len(sent) == SEND_BUDGET < 5
    assert sent[-1].endswith(f"\n后面还有 35 段，回“/read {EID} 4”接着看。")
    assert "文件" not in sent[-1] and "很长" not in sent[-1]
    assert app.outbox.count() == 0


async def test_clawbot_keeps_the_smaller_of_its_budget_and_the_batch(tmp_path):
    app, ch = clawbot_app(tmp_path)
    app.cfg.read_batch_parts = 2
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    await long_entry(app)
    await wechat(app, ch, "/read b305ad")
    sent = [t for _, t, _ in ch._client.sent]
    assert len(sent) == 2 and sent[-1].endswith(f"回“/read {EID} 3”接着看。")


async def test_clawbot_read_file_says_wechat_cannot_and_sends_parts(tmp_path):
    app, ch = clawbot_app(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    await long_entry(app)
    await wechat(app, ch, "/read b305ad 文件")
    sent = [t for _, t, _ in ch._client.sent]
    assert len(sent) == SEND_BUDGET and sent[0].startswith("微信里没法发文件，改成分段发。\n\n（1/38）")


# ---- English ----

async def test_english_labels_hints_and_words(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    i18n.set_language("en")  # after the config, which sets its own
    await long_entry(app)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read b305ad"))
    cont, whole = buttons_of(ch)[-1]
    assert (cont.label, whole.label) == ("Continue (6/38)", "Send as a file")
    assert whole.fallback == f'Reply "/read {EID} file" for the whole entry as a file.'
    assert texts(ch)[-1].endswith('This one is long (38 parts): tap "Send as a file" to get it all at once.')
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read b305ad 文件"))  # either language
    assert len(ch.files) == 1
    ch.reason = "too-big"
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read b305ad file"))
    assert texts(ch)[5].startswith("Too big to send as a file; sending it in parts instead.\n\n")


# ---- callback data and help ----

def test_callback_data_fits_every_platform():
    from octopus.app import App

    for data in (b.data for b in App._read_buttons(EID, 999999, 999999, True)):
        assert len(data.encode()) <= 64 and len(data) <= MAX_CUSTOM_ID  # Telegram 64 bytes, Discord 100
        assert len(card_json("x", [Button("x", data, "")])["body"]["elements"][1]["behaviors"][0]["value"]["fix"]) < 100


def test_help_lists_the_new_read_forms():
    assert "/read 记录号 文件 — 整篇发成文件（微信里不行）；/read 记录号 全部 — 一次全发完" in HELP
    assert "/read 记录号 — 在聊天里看这条的全文（长的一次发几段，点【继续】接着看）" in HELP


def test_read_batch_parts_setting(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("", encoding="utf-8")
    assert load_config(p).read_batch_parts == 5
    p.write_text("read_batch_parts = 3\n", encoding="utf-8")
    assert load_config(p).read_batch_parts == 3
    for bad in ("0", "true", '"5"', "2.5"):
        p.write_text(f"read_batch_parts = {bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="read_batch_parts"):
            load_config(p)


# ---- each channel's file and buttons ----

async def test_telegram_sends_a_document_and_refuses_too_big(tmp_path):
    class Bot:
        def __init__(self):
            self.docs = []

        async def send_document(self, chat_id, document, filename=None):
            self.docs.append((chat_id, document, filename))
            return SimpleNamespace(message_id=7)

    bot = Bot()
    ch = TelegramChannel("t", tmp_path, bot=bot)
    r = await ch.send_file("42", b"# note", "a-b305ad.md")
    assert r.ok and r.message_id == "7" and bot.docs == [(42, b"# note", "a-b305ad.md")]
    r = await ch.send_file("42", b"x" * (TELEGRAM_MAX + 1), "big.md")
    assert not r.ok and r.reason == "too-big" and len(bot.docs) == 1

    class Broken(Bot):
        async def send_document(self, *a, **kw):
            raise RuntimeError("network")

    assert (await TelegramChannel("t", tmp_path, bot=Broken()).send_file("42", b"x", "a.md")).reason == "error"


async def test_discord_sends_an_attachment_and_refuses_too_big(tmp_path):
    from test_discord import OWNER as D_OWNER
    from test_discord import FakeClient as DiscordClient
    from test_discord import channel

    ch = channel(tmp_path)
    r = await ch.send_file(D_OWNER, b"# note", "a-b305ad.md")
    content, _, file = ch.client.dm.sent[-1]
    assert r.ok and content is None and file.filename == "a-b305ad.md" and file.fp.read() == b"# note"
    r = await ch.send_file(D_OWNER, b"x" * (DISCORD_MAX + 1), "big.md")
    assert r.reason == "too-big" and len(ch.client.dm.sent) == 1
    assert (await channel(tmp_path, client=DiscordClient(ready=False)).send_file(D_OWNER, b"x", "a.md")).reason \
        == "not-connected"


async def test_feishu_sends_a_file_message_and_refuses_too_big(tmp_path):
    from test_feishu import FakeApi

    class Api(FakeApi):
        def __init__(self):
            super().__init__()
            self.files = []

        async def send_file(self, open_id, data, filename):
            self.files.append((open_id, data, filename))
            return "om_file"

    api = Api()
    ch = FeishuChannel("a", "s", tmp_path, api=api)
    r = await ch.send_file("ou_me", b"# note", "a-b305ad.md")
    assert r.ok and r.message_id == "om_file" and api.files == [("ou_me", b"# note", "a-b305ad.md")]
    assert (await ch.send_file("ou_me", b"x" * (FEISHU_MAX + 1), "big.md")).reason == "too-big"

    class Broken(FakeApi):
        async def send_file(self, *a):
            raise RuntimeError("boom")

    assert (await FeishuChannel("a", "s", tmp_path, api=Broken()).send_file("ou_me", b"x", "a.md")).reason == "error"


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # lark-oapi's bundled protobuf, on import
async def test_feishu_api_uploads_then_sends_a_file_message():
    from octopus.channels.feishu import LarkApi

    api = LarkApi.__new__(LarkApi)
    uploaded, created = [], []

    def create(req):
        uploaded.append((req.request_body.file_type, req.request_body.file_name, req.request_body.file.read()))
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(file_key="fk_1"))

    api.client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(file=SimpleNamespace(create=create))))

    async def _create(open_id, msg_type, content):
        created.append((open_id, msg_type, content))
        return "om_1"

    api._create = _create
    assert await api.send_file("ou_me", b"# note", "a-b305ad.md") == "om_1"
    assert uploaded == [("stream", "a-b305ad.md", b"# note")]
    assert created == [("ou_me", "file", {"file_key": "fk_1"})]


def _telegram_app(tmp_path):
    from test_telegram import FakeBot

    class Bot(FakeBot):
        async def send_document(self, chat_id, document, filename=None):
            self.sent.append((chat_id, f"[file {filename}]", None))
            return SimpleNamespace(message_id=1)

    ch = TelegramChannel("t", tmp_path / "in", bot=Bot())
    return ch, lambda: [(text, markup) for _, text, markup in ch.bot.sent]


def _discord_app(tmp_path):
    from test_discord import channel

    ch = channel(tmp_path / "in")
    return ch, lambda: [(content if content is not None else f"[file {file.filename}]", view)
                        for content, view, file in ch.client.dm.sent]


def _feishu_app(tmp_path):
    from test_feishu import FakeApi

    class Api(FakeApi):
        def __init__(self):
            super().__init__()
            self.order, self.plain = [], []

        async def send_text(self, open_id, text):
            self.order.append((text, None))
            return await super().send_text(open_id, text)

        async def send_card(self, open_id, markdown, buttons, plain=False):
            self.order.append((markdown, buttons))
            self.plain.append(plain)
            return await super().send_card(open_id, markdown, buttons, plain)

        async def send_file(self, open_id, data, filename):
            self.order.append((f"[file {filename}]", None))
            return "om_file"

    ch = FeishuChannel("a", "s", tmp_path / "in", api=Api())
    return ch, lambda: ch.api.order


def _labels(name, extra):
    """The button labels and data a channel put on a message."""
    if extra is None:
        return []
    if name == "telegram":
        return [(b.text, b.callback_data) for row in extra.inline_keyboard for b in row]
    if name == "discord":
        return [(b.label, b.custom_id) for b in extra.children]
    return [(b.label, b.data) for b in extra]


@pytest.mark.parametrize("make", [_telegram_app, _discord_app, _feishu_app])
async def test_each_channel_gets_a_batch_with_buttons_and_the_file(tmp_path, make):
    ch, out = make(tmp_path)
    app, _, _ = make_app(tmp_path, channel=ch)
    user = "42" if ch.name == "telegram" else "123456789012345678" if ch.name == "discord" else "ou_me"
    app.cfg.channels[ch.name] = ChannelConfig(enabled=True, allowed_users=[user])
    await long_entry(app)
    await app.handle(IncomingMessage(channel=ch.name, user_id=user, text="/read b305ad"))
    sent = out()
    assert [text.split("\n", 1)[0] for text, _ in sent] == [f"（{k}/38）" for k in range(1, 6)]
    assert [_labels(ch.name, extra) for _, extra in sent[:4]] == [[]] * 4
    assert _labels(ch.name, sent[4][1]) == [("继续（6/38）", f"read:{EID}:6"), ("整篇发文件", f"read:{EID}:file")]
    await app.handle(IncomingMessage(channel=ch.name, user_id=user, text="", callback=f"read:{EID}:file"))
    assert out()[5][0] == "[file 长文-b305ad.md]" and len(out()) == 6
    await app.handle(IncomingMessage(channel=ch.name, user_id=user, text="", callback=f"read:{EID}:6"))
    assert out()[6][0].startswith("（6/38）") and len(out()) == 11


# ---- P35 fix round 1 ----

async def test_the_same_read_tap_twice_within_seconds_sends_once(tmp_path):
    """Telegram and Discord keep the buttons (and have no dedupe of their own): a double tap is one batch."""
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    await long_entry(app)
    now = [100.0]
    app.monotonic = lambda: now[0]
    await app.handle(tap(f"read:{EID}:6"))
    await app.handle(tap(f"read:{EID}:6"))
    assert len(texts(ch)) == 5
    await app.handle(tap(f"read:{EID}:file"))  # another button of that message: not a repeat
    assert len(ch.files) == 1
    now[0] += READ_TAP_REPEAT_SECONDS + 0.1
    await app.handle(tap(f"read:{EID}:6"))  # later on purpose: read again
    assert len(texts(ch)) == 10
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=f"/read {EID} 6"))  # typed: never skipped
    assert len(texts(ch)) == 15


def test_note_filename_is_the_title_and_the_short_id():
    from datetime import datetime

    from octopus.library import Entry, note_filename

    def name(title):
        return note_filename(Entry(id=EID, title=title, source="manual", channel="tg", saved_at=datetime(2026, 10, 15)))

    assert name("Acme 宣布：迁移 Rust?") == "Acme-宣布-迁移-Rust-b305ad.md"
    assert name("../../etc/passwd") == "..-..-etc-passwd-b305ad.md"  # no separators: one bare name
    assert name("？？") == "untitled-b305ad.md"
    assert name("长" * 100) == "长" * 30 + "-b305ad.md"


async def test_clawbot_keeps_a_blank_line_before_the_continue_hint(tmp_path):
    app, ch = clawbot_app(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    await long_entry(app)
    await wechat(app, ch, "/read b305ad")
    last = [t for _, t, _ in ch._client.sent][-1]
    assert last.endswith("字\n\n" + f"后面还有 35 段，回“/read {EID} 4”接着看。")


async def test_clawbot_with_no_budget_holds_the_part_with_its_hint_after_a_blank_line(tmp_path):
    app, ch = clawbot_app(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    await long_entry(app)
    for _ in range(SEND_BUDGET):
        await wechat(app, ch, "/status")
    await wechat(app, ch, "/read b305ad")
    held = [t for _, t, _ in app.outbox.queued("clawbot", OWNER)]
    assert len(held) == 1 and held[0].endswith("字\n\n" + f"后面还有 37 段，回“/read {EID} 2”接着看。")


async def test_feishu_shows_the_batchs_last_part_as_plain_text_like_the_others(tmp_path):
    ch, out = _feishu_app(tmp_path)
    app, _, _ = make_app(tmp_path, channel=ch)
    app.cfg.channels["feishu"] = ChannelConfig(enabled=True, allowed_users=["ou_me"])
    await seed(app, EID, "长文", "\n\n".join(f"第{i}段 *不是强调* [x](y) " + "字" * 1700 for i in range(1, 9)))
    await app.handle(IncomingMessage(channel="feishu", user_id="ou_me", text="/read b305ad"))
    markdown, buttons = ch.api.order[4]
    assert buttons and ch.api.cards[-1][2] == buttons
    card = card_json(markdown, buttons, plain=True)
    first = card["body"]["elements"][0]
    assert first == {"tag": "div", "text": {"tag": "plain_text", "content": markdown}}
    assert card_json("x", [])["body"]["elements"][0] == {"tag": "markdown", "content": "x"}  # other replies as before
    assert ch.api.plain == [True]


@pytest.mark.parametrize("text,words", [("/read file", "file"), ("/read 文件", "file"), ("/read 全部", "all"),
                                        ("/read ALL", "all")])
async def test_read_word_alone_means_the_current_entry(tmp_path, text, words):
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    await long_entry(app, parts=12)
    app.focus.set("tg", "me", EID)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=text))
    if words == "file":
        assert len(ch.files) == 1 and ch.sent == []
    else:
        assert len(texts(ch)) == 12


async def test_read_word_alone_without_a_current_entry_is_the_usage(tmp_path):
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/read file"))
    assert texts(ch) == [READ_USAGE]


async def test_a_double_tap_queued_behind_a_slow_batch_is_still_skipped(tmp_path):
    """The window runs from the end of the batch: a second tap that waited for the first batch's pauses is a repeat
    (P35 re-review R-1)."""
    app, ch, _ = make_app(tmp_path, channel=FileChannel())
    await long_entry(app)
    now = [100.0]
    app.monotonic = lambda: now[0]

    async def sleep(seconds):
        now[0] += seconds

    app.sleep = sleep
    app.cfg.read_batch_parts = 8  # 7 s of pauses: longer than the window
    await app.handle(tap(f"read:{EID}:6"))
    assert now[0] - 100.0 > READ_TAP_REPEAT_SECONDS
    await app.handle(tap(f"read:{EID}:6"))  # tapped at once, handled only now
    assert len(texts(ch)) == 8
    now[0] += READ_TAP_REPEAT_SECONDS + 0.1  # on purpose, after the batch ended
    await app.handle(tap(f"read:{EID}:6"))
    assert len(texts(ch)) == 16
