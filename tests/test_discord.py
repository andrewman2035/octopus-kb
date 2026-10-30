"""P23: the Discord channel (owner-only DMs over the gateway), driven by fake messages and a fake client."""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from octopus.bootstrap import build_channels
from octopus.channels.base import Button, Reply, user_tag
from octopus.channels.discord import (DOWNLOAD_FAILED, KEEP_ALIVE_SECONDS, MAX_DOWNLOAD, MAX_TEXT, SLASH_COMMANDS,
                                      TOO_BIG, DiscordChannel, command_payload)
from octopus.config import ChannelConfig, ConfigError, load_config, make_test_config

OWNER = "123456789012345678"
CJK = re.compile("[一-鿿]")


class FakeAttachment:
    def __init__(self, filename="a.png", content_type="image/png", size=10, data=b"bytes", voice=False, fail=False):
        self.filename, self.content_type, self.size = filename, content_type, size
        self.data, self.voice, self.fail = data, voice, fail

    def is_voice_message(self):
        return self.voice

    async def save(self, fp):
        if self.fail:
            raise OSError("download failed")
        open(fp, "wb").write(self.data)


class FakeDM:
    def __init__(self):
        self.sent, self.n = [], 500
        self.fail_at = None

    async def send(self, content=None, view=None, file=None):
        self.n += 1
        if self.fail_at is not None and len(self.sent) >= self.fail_at:
            raise RuntimeError("network")
        self.sent.append((content, view, file))
        return SimpleNamespace(id=self.n)


class FakeClient:
    def __init__(self, ready=True):
        self.dm = FakeDM()
        self.ready = ready
        self.fetched = []

    def is_ready(self):
        return self.ready

    def get_user(self, uid):
        return None

    async def fetch_user(self, uid):
        self.fetched.append(uid)
        return SimpleNamespace(id=uid, dm_channel=None, create_dm=self._create_dm)

    async def _create_dm(self):
        return self.dm


def message(content="", author=OWNER, attachments=(), guild=None, reference=None, bot=False, mid=7):
    return SimpleNamespace(content=content, author=SimpleNamespace(id=int(author), bot=bot), attachments=list(attachments),
                           guild=guild, reference=reference, id=mid, stickers=[])


def channel(tmp_path, client=None, owners=(OWNER,)):
    return DiscordChannel("tok", tmp_path, client=client or FakeClient(), allowed=lambda uid: uid in owners)


async def test_a_dm_from_the_owner_is_a_message(tmp_path):
    ch = channel(tmp_path)
    m = await ch.to_incoming(message("你好", reference=SimpleNamespace(message_id=3)))
    assert (m.channel, m.user_id, m.text, m.message_id, m.reply_to_id) == ("discord", OWNER, "你好", "7", "3")


async def test_others_are_ignored_with_one_log_line(tmp_path, caplog):
    ch = channel(tmp_path)
    caplog.set_level(logging.INFO)
    stranger = "999999999999999999"
    assert await ch.to_incoming(message("hi", author=stranger)) is None
    assert await ch.to_incoming(message("hi again", author=stranger)) is None
    lines = [r.getMessage() for r in caplog.records if "ignor" in r.getMessage()]
    assert len(lines) == 1 and user_tag(stranger) in lines[0] and stranger not in lines[0]
    assert ch.client.dm.sent == []  # never answered


async def test_server_messages_and_bots_are_ignored(tmp_path):
    ch = channel(tmp_path)
    assert await ch.to_incoming(message("hi", guild=SimpleNamespace(id=1))) is None
    assert await ch.to_incoming(message("hi", bot=True)) is None


async def test_attachments_are_downloaded_by_kind(tmp_path):
    ch = channel(tmp_path)
    m = await ch.to_incoming(message("看图", attachments=[
        FakeAttachment("p.png", "image/png"), FakeAttachment("论文.pdf", "application/pdf"),
        FakeAttachment("clip.mp4", "video/mp4"), FakeAttachment("talk.mp3", "audio/mpeg")]))
    assert [a.kind for a in m.attachments] == ["image", "file", "video", "audio"]
    assert all(a.path.read_bytes() == b"bytes" and a.path.parent.parent == tmp_path for a in m.attachments)
    assert m.attachments[1].filename == "论文.pdf" and not any(a.voice for a in m.attachments)


async def test_a_voice_message_goes_the_voice_path(tmp_path):
    ch = channel(tmp_path)
    m = await ch.to_incoming(message(attachments=[FakeAttachment("voice-message.ogg", "audio/ogg", voice=True)]))
    [a] = m.attachments
    assert a.kind == "audio" and a.voice is True


async def test_a_file_name_cannot_leave_the_staging_folder(tmp_path):
    ch = channel(tmp_path)
    m = await ch.to_incoming(message(attachments=[FakeAttachment("../../evil.txt", "text/plain")]))
    assert m.attachments[0].path.name == "evil.txt" and m.attachments[0].path.parent.parent == tmp_path


async def test_too_big_is_told_and_nothing_kept(tmp_path):
    ch = channel(tmp_path)
    m = await ch.to_incoming(message(attachments=[FakeAttachment(), FakeAttachment(size=MAX_DOWNLOAD + 1)]))
    assert m is None and ch.client.dm.sent[-1][0] == TOO_BIG
    assert list(tmp_path.iterdir()) == []


async def test_a_failed_download_is_told(tmp_path):
    ch = channel(tmp_path)
    assert await ch.to_incoming(message(attachments=[FakeAttachment(fail=True)])) is None
    assert ch.client.dm.sent[-1][0] == DOWNLOAD_FAILED and list(tmp_path.iterdir()) == []


async def test_nothing_readable_is_dropped(tmp_path, caplog):
    ch = channel(tmp_path)
    caplog.set_level(logging.INFO)
    assert await ch.to_incoming(message("")) is None
    assert any("not handed over: nothing readable" in r.getMessage() for r in caplog.records)


async def test_send_splits_at_the_discord_limit_with_buttons_on_the_last_part(tmp_path):
    ch = channel(tmp_path)
    r = await ch.send(OWNER, Reply("x" * (MAX_TEXT + 10), [Button("改成提问", "fix:1", "回复“问”改成提问"),
                                                            Button("确认", "op:3:yes", "回复“确认”")]))
    sent = ch.client.dm.sent
    assert MAX_TEXT == 2000 and r.ok and r.message_id == "502"
    assert len(sent) == 2 and sent[0][1] is None and all(len(c) <= MAX_TEXT for c, _, _ in sent)
    view = sent[1][1]
    assert [(b.label, b.custom_id) for b in view.children] == [("改成提问", "fix:1"), ("确认", "op:3:yes")]
    assert ch.client.fetched == [int(OWNER)]


async def test_send_before_the_gateway_is_up_is_held(tmp_path):
    ch = channel(tmp_path, client=FakeClient(ready=False))
    r = await ch.send(OWNER, Reply("hi"))
    assert not r.ok and r.reason == "not-connected"
    ch.client = None
    assert (await ch.send(OWNER, Reply("hi"))).reason == "not-connected"


async def test_send_failure_and_partial_failure(tmp_path):
    ch = channel(tmp_path)
    ch.client.dm.fail_at = 0
    r = await ch.send(OWNER, Reply("hi"))
    assert not r.ok and r.reason == "error"
    ch.client.dm.fail_at = 1  # the first part went out: not re-sent as a whole
    assert (await ch.send(OWNER, Reply("y" * (MAX_TEXT + 5)))).ok


async def test_send_image(tmp_path):
    ch = channel(tmp_path)
    img = tmp_path / "qr.png"
    img.write_bytes(b"png")
    r = await ch.send_image(OWNER, img, "扫码")
    content, _, file = ch.client.dm.sent[-1]
    assert r.ok and content == "扫码" and file.filename == "qr.png"


class FakeResponse:
    def __init__(self, fail=None, gate=None):
        self.calls = []
        self.fail = fail  # what answering raises (Discord's 10062 when the 3 s are over)
        self.gate = gate  # answering waits for it (an answer on its way; never set: a connection that hangs)

    async def _answer(self, call):
        self.calls.append(call)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail

    async def edit_message(self, view=None):
        await self._answer(("edit", view))

    async def defer(self):
        await self._answer(("defer", None))

    async def send_message(self, content=None, **kw):
        await self._answer(("send", content, kw.get("silent")))


def tap(custom_id, user=OWNER, fail=None, created_at=None):
    return SimpleNamespace(id=900, data={"custom_id": custom_id, "component_type": 2},
                           user=SimpleNamespace(id=int(user)), response=FakeResponse(fail),
                           created_at=created_at or datetime.now(timezone.utc))


async def test_a_tap_is_a_callback_and_its_buttons_go(tmp_path):
    ch = channel(tmp_path)
    t = tap("split:4")
    m = await ch.on_callback(t)
    assert (m.channel, m.user_id, m.callback, m.text) == ("discord", OWNER, "split:4", "")
    assert t.response.calls == [("edit", None)]


async def test_the_guide_buttons_stay(tmp_path):
    ch = channel(tmp_path)
    t = tap("cmd:recent")
    assert (await ch.on_callback(t)).callback == "cmd:recent"
    assert t.response.calls == [("defer", None)]


@pytest.mark.parametrize("data", ["read:20261015-b305ad:6", "read:20261015-b305ad:file"])
async def test_the_read_buttons_stay(tmp_path, data):
    """P35: tapping "Send as a file" keeps "Continue" on that message, and the other way round."""
    ch = channel(tmp_path)
    t = tap(data)
    assert (await ch.on_callback(t)).callback == data
    assert t.response.calls == [("defer", None)]


async def test_a_strangers_tap_is_ignored(tmp_path):
    ch = channel(tmp_path)
    t = tap("op:1:yes", user="999")
    assert await ch.on_callback(t) is None and t.response.calls == []


async def test_messages_reach_the_app_in_order_per_user(tmp_path):
    """A slow download never lets a later text overtake it (as on Telegram)."""
    ch = channel(tmp_path)
    gate = asyncio.Event()

    class Slow(FakeAttachment):
        async def save(self, fp):
            await gate.wait()
            await super().save(fp)

    got = []

    async def on_message(m):
        got.append(m.text)

    await ch.handle_message(message("图", attachments=[Slow()]), on_message)
    await ch.handle_message(message("后面的字"), on_message)
    await asyncio.sleep(0.05)
    assert got == []
    gate.set()
    await ch.drain()
    assert got == ["图", "后面的字"]


# ---- config and wiring ----

def test_config_owner_ids_are_the_allow_list(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[channels.discord]\nenabled = true\nowner_user_ids = [123456789012345678, "42"]\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.channels["discord"] == ChannelConfig(enabled=True, allowed_users=["123456789012345678", "42"])


@pytest.mark.parametrize("body", ['[channels.discord]\nallowed_users = ["1"]\n',
                                  '[channels.discord]\nowner_user_ids = "1"\n'])
def test_config_discord_mistakes_are_told(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


class Secrets:
    def __init__(self, **values):
        self.values = values

    def get(self, name):
        return self.values.get(name)


def test_build_channels_needs_the_bot_token(tmp_path):
    cfg = make_test_config(tmp_path)
    cfg.channels["discord"] = ChannelConfig(enabled=True, allowed_users=[OWNER])
    channels, problems = build_channels(cfg, Secrets(), notify=None)
    assert "discord" not in channels and "discord_bot_token" in problems[0]
    channels, problems = build_channels(cfg, Secrets(discord_bot_token="t"), notify=None)
    assert isinstance(channels["discord"], DiscordChannel) and problems == []
    assert channels["discord"].allowed(OWNER) and not channels["discord"].allowed("1")


def test_off_by_default(tmp_path):
    cfg = make_test_config(tmp_path)
    assert "discord" not in cfg.channels
    assert build_channels(cfg, Secrets(discord_bot_token="t"), notify=None) == ({}, [])


# ---- through App, and the gateway wiring (a fake discord.Client, never a connection) ----

async def test_an_owner_link_is_saved_and_answered_on_discord(tmp_path, caplog):
    from test_app import ARTICLE, make_app

    from fakes import StubFetcher

    ch = channel(tmp_path)
    app, _, _ = make_app(tmp_path, StubFetcher(ARTICLE), channel=ch)
    app.cfg.channels["discord"] = ChannelConfig(enabled=True, allowed_users=[OWNER])
    caplog.set_level(logging.INFO)
    await app.handle(await ch.to_incoming(message("https://a.com/x")))
    await app.process_due_jobs()
    texts = [c for c, _, _ in ch.client.dm.sent]
    assert texts[0] == "收到，处理中…" and texts[1].startswith("✅ 已保存：复利的力量")
    logged = [r.getMessage() for r in caplog.records]
    assert any(line.startswith(f"in discord {user_tag(OWNER)} save_urls") for line in logged)
    assert any(line.startswith(f"out discord {user_tag(OWNER)}") and line.endswith(" ok") for line in logged)
    assert not any(OWNER in line for line in logged)


async def test_owner_notices_reach_discord(tmp_path):
    from test_app import make_app

    ch = channel(tmp_path)
    app, _, _ = make_app(tmp_path, channel=ch)
    app.cfg.channels["discord"] = ChannelConfig(enabled=True, allowed_users=[OWNER])
    assert await app.notify_owner("微信要重新登录") == 1
    assert ch.client.dm.sent[-1][0] == "微信要重新登录"


async def test_start_wires_the_events_and_says_when_the_token_is_rejected(tmp_path, monkeypatch):
    import discord

    import aiohttp

    got = []

    class FakeConnector:
        def __init__(self, **kw):
            self.kw = kw

        async def close(self):
            pass

    monkeypatch.setattr(aiohttp, "TCPConnector", FakeConnector)

    class FakeGatewayClient:
        def __init__(self, intents, connector=None):
            self.intents, self.events = intents, {}
            # P30: an idle connection to Discord is kept, not closed after aiohttp's 15 s: through a proxy a new
            # one took 2-5 s, and a tap's answer has 3; but an idle one was found silently dead after 400 s (good at 240)
            assert isinstance(connector, FakeConnector) and connector.kw == {"limit": 0,
                                                                             "keepalive_timeout": KEEP_ALIVE_SECONDS}
            assert 60 <= KEEP_ALIVE_SECONDS < 240
            self.application_id, self.http = 42, http

        def event(self, fn):
            self.events[fn.__name__] = fn
            return fn

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def start(self, token):
            # M5: only DMs; their words come without the privileged Message Content intent
            assert token == "tok" and self.intents.dm_messages
            assert not self.intents.guild_messages and not self.intents.message_content
            assert self.intents.guilds  # without it discord.py warns on every start; not privileged, little traffic
            await self.events["on_message"](message("你好"))
            await self.events["on_interaction"](SimpleNamespace(type=discord.InteractionType.component,
                                                                **vars(tap("cmd:help"))))
            await self.events["on_interaction"](SimpleNamespace(type=discord.InteractionType.application_command,
                                                                **vars(command("recent", "3"))))
            await self.events["on_ready"]()
            await self.events["on_ready"]()  # again after a reconnect: the commands are not checked again
            await asyncio.sleep(0.05)
            raise discord.LoginFailure("bad token")

    async def on_message(m):
        got.append((m.text, m.callback))

    http = FakeHTTP()
    monkeypatch.setattr(discord, "Client", FakeGatewayClient)
    ch = DiscordChannel("tok", tmp_path, allowed=lambda uid: uid == OWNER)
    with pytest.raises(RuntimeError, match="token 被拒绝"):
        await ch.start(on_message)
    assert sorted(got) == [("", "cmd:help"), ("/recent 3", None), ("你好", None)]
    assert [c[0] for c in http.calls] == ["GET", "PUT"]


async def test_the_owners_server_message_is_ignored_with_one_log_line(tmp_path, caplog):
    ch = channel(tmp_path)
    caplog.set_level(logging.INFO)
    for _ in range(2):
        assert await ch.to_incoming(message("hi", guild=SimpleNamespace(id=1))) is None
    lines = [r.getMessage() for r in caplog.records if "server message" in r.getMessage()]
    assert len(lines) == 1 and user_tag(OWNER) in lines[0] and OWNER not in lines[0]


async def test_a_tap_waits_for_the_message_sent_before_it(tmp_path):
    """M5: a tap right after a slow file never reaches App before that file's message."""
    ch = channel(tmp_path)
    gate = asyncio.Event()

    class Slow(FakeAttachment):
        async def save(self, fp):
            await gate.wait()
            await super().save(fp)

    got = []

    async def on_message(m):
        got.append(m.callback or m.text)

    await ch.handle_message(message("图", attachments=[Slow()]), on_message)
    await ch.handle_callback(tap("cmd:recent"), on_message)
    await asyncio.sleep(0.05)
    assert got == []
    gate.set()
    await ch.drain()
    assert got == ["图", "cmd:recent"]


async def test_a_tap_outside_a_dm_is_ignored(tmp_path):
    ch = channel(tmp_path)
    t = tap("op:1:yes")
    t.guild_id = 99
    assert await ch.on_callback(t) is None and t.response.calls == []


# ---- P30: a tap is answered before anything waits; the answer's timing is logged ----

async def test_a_tap_is_answered_before_it_waits_for_the_message_sent_before_it(tmp_path):
    ch = channel(tmp_path)
    gate = asyncio.Event()

    class Slow(FakeAttachment):
        async def save(self, fp):
            await gate.wait()
            await super().save(fp)

    got = []

    async def on_message(m):
        got.append(m.callback or m.text)

    await ch.handle_message(message("图", attachments=[Slow()]), on_message)
    t = tap("cmd:status")
    await ch.handle_callback(t, on_message)
    assert t.response.calls == [("defer", None)] and got == []  # answered in the gateway handler itself
    gate.set()
    await ch.drain()
    assert got == ["图", "cmd:status"]


async def test_a_tap_answered_too_late_is_still_handled_and_says_how_late(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="octopus.channels.discord")
    ch = channel(tmp_path)
    got = []

    async def on_message(m):
        got.append(m.callback)

    late = tap("cmd:status", fail=RuntimeError("404 Not Found (error code: 10062): Unknown interaction"),
               created_at=datetime.now(timezone.utc) - timedelta(seconds=3.4))
    await ch.handle_callback(late, on_message)
    await ch.drain()
    assert got == ["cmd:status"]
    lines = [r.getMessage() for r in caplog.records if "could not answer" in r.getMessage()]
    assert len(lines) == 1 and not any(r.exc_info for r in caplog.records)
    line = lines[0]
    assert line.startswith(f"discord: could not answer a tap from {user_tag(OWNER)} (cmd:status): ")
    assert "RuntimeError: 404 Not Found (error code: 10062): Unknown interaction" in line
    assert "3.4 s after it was made" in line and re.search(r"the answer took \d+\.\d s", line) and OWNER not in line


async def test_an_answered_tap_logs_its_timing_at_debug(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="octopus.channels.discord")
    ch = channel(tmp_path)
    await ch.on_callback(tap("cmd:recent", created_at=datetime.now(timezone.utc) - timedelta(seconds=0.4)))
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG and "answered a tap" in r.getMessage()]
    assert len(debug) == 1 and "0.4 s after it was made" in debug[0].getMessage()


async def test_a_tap_without_a_creation_time_is_still_answered(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="octopus.channels.discord")
    ch = channel(tmp_path)
    t = tap("cmd:recent", fail=RuntimeError("late"))
    del t.created_at
    assert (await ch.on_callback(t)).callback == "cmd:recent"
    assert any("? s after it was made" in r.getMessage() for r in caplog.records)


# ---- P30: our commands in Discord's "/" menu ----

def command(name, text=None, user=OWNER, fail=None, guild_id=None):
    options = [{"name": "text", "type": 3, "value": text}] if text is not None else []
    return SimpleNamespace(id=901, data={"name": name, "type": 1, "options": options}, guild_id=guild_id,
                           user=SimpleNamespace(id=int(user)), response=FakeResponse(fail),
                           created_at=datetime.now(timezone.utc))


async def test_a_slash_command_is_answered_then_handed_over_as_typed(tmp_path):
    ch = channel(tmp_path)
    got = []

    async def on_message(m):
        got.append(m)

    c = command("s", "  明天给 Sam 回电话 ")
    await ch.handle_command(c, on_message)
    assert c.response.calls == [("send", "/s 明天给 Sam 回电话", True)]  # the line, silently, as the answer
    await ch.drain()
    assert [(m.channel, m.user_id, m.text, m.callback) for m in got] == [("discord", OWNER, "/s 明天给 Sam 回电话", None)]


async def test_a_slash_command_without_text_and_a_long_one(tmp_path):
    ch = channel(tmp_path)
    c = command("recent")
    assert (await ch.on_command(c)).text == "/recent" and c.response.calls[0][1] == "/recent"
    long = command("s", "字" * 5000)
    m = await ch.on_command(long)
    assert m.text == "/s " + "字" * 5000  # App gets it all; the echo fits one message
    assert len(long.response.calls[0][1]) == MAX_TEXT and long.response.calls[0][1].endswith("…")


async def test_a_slash_command_answered_too_late_is_still_handled(tmp_path):
    ch = channel(tmp_path)
    got = []

    async def on_message(m):
        got.append(m.text)

    await ch.handle_command(command("status", fail=RuntimeError("10062")), on_message)
    await ch.drain()
    assert got == ["/status"]


async def test_strangers_and_server_slash_commands_are_ignored(tmp_path):
    ch = channel(tmp_path)
    stranger = command("status", user="999")
    server = command("status", guild_id=5)
    assert await ch.on_command(stranger) is None and stranger.response.calls == []
    assert await ch.on_command(server) is None and server.response.calls == []


async def test_a_slash_command_keeps_its_place_among_messages(tmp_path):
    """Before it: a slow file waits for nothing behind it; after it: a typed text waits for it, as typed lines do."""
    ch = channel(tmp_path)
    gate = asyncio.Event()

    class Slow(FakeAttachment):
        async def save(self, fp):
            await gate.wait()
            await super().save(fp)

    got = []

    async def on_message(m):
        got.append(m.text)

    await ch.handle_message(message("图", attachments=[Slow()]), on_message)
    await ch.handle_command(command("s", "一条笔记"), on_message)
    await ch.handle_message(message("后面的字"), on_message)
    await asyncio.sleep(0.05)
    assert got == []
    gate.set()
    await ch.drain()
    assert got == ["图", "/s 一条笔记", "后面的字"]


def test_the_command_payload():
    payload = command_payload()
    names = [c["name"] for c in payload]
    assert names == [name for name, _ in SLASH_COMMANDS]
    assert {"s", "add", "new", "done", "recent", "list", "read", "find", "delete", "resummarize", "model", "status",
            "clawbot", "help"} == set(names)
    for c in payload:
        assert c["type"] == 1 and c["contexts"] == [1] and c["integration_types"] == [0]  # DMs with the bot only
        assert 1 <= len(c["description"]) <= 100 and 1 <= len(c["description_localizations"]["zh-CN"]) <= 100
        assert not CJK.search(c["description"]) and CJK.search(c["description_localizations"]["zh-CN"])
        assert 1 <= len(c["description_localizations"]["zh-TW"]) <= 100
    by_name = {c["name"]: c for c in payload}
    assert [o["name"] for o in by_name["s"]["options"]] == ["text"]
    option = by_name["s"]["options"][0]
    assert option["type"] == 3 and option["name_localizations"] == {"zh-CN": "内容", "zh-TW": "內容"}
    # a Traditional Chinese client gets the Chinese text in traditional characters, not English
    assert by_name["s"]["description_localizations"]["zh-TW"] == "把文字存成新的一條筆記"
    assert option["description_localizations"]["zh-TW"] == "命令後面的內容"
    # without its text the command is useless: Discord's box goes into the field and will not send it empty
    required = {name for name, c in by_name.items() if c.get("options") and c["options"][0]["required"]}
    assert required == {"s", "add", "read", "find", "delete", "resummarize"}
    assert {name for name, c in by_name.items() if c.get("options")} - required == {"recent", "list", "model"}
    assert "options" not in by_name["status"] and "options" not in by_name["help"]


class FakeHTTP:
    def __init__(self, current=None, fail=None):
        self.current, self.fail, self.calls = current or [], fail, []

    async def request(self, route, **kw):
        self.calls.append((route.method, route.path, kw.get("params")))
        if self.fail is not None:
            raise self.fail
        return self.current

    async def bulk_upsert_global_commands(self, application_id, payload):
        self.calls.append(("PUT", application_id, len(payload)))
        self.current = payload
        return payload


def as_discord_returns(payload):
    """What GET …/commands?with_localizations=true gives back: ids, versions, nulls, a false required omitted."""
    out = []
    for i, c in enumerate(payload):
        c = {**c, "id": str(i), "application_id": "42", "version": "1", "default_member_permissions": None,
             "nsfw": False, "name_localizations": None, "dm_permission": True}
        if "options" in c:
            c["options"] = [{k: v for k, v in o.items() if k != "required" or v} for o in c["options"]]
        out.append(c)
    return out


def sync_channel(tmp_path, http):
    client = FakeClient()
    client.application_id, client.http = 42, http
    return channel(tmp_path, client=client)


async def test_commands_are_registered_when_discord_has_others(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="octopus.channels.discord")
    http = FakeHTTP(current=[{"id": "1", "name": "old", "type": 1, "description": "x"}])
    await sync_channel(tmp_path, http).sync_commands()
    assert http.calls[0] == ("GET", "/applications/{application_id}/commands", {"with_localizations": "true"})
    assert http.calls[1] == ("PUT", 42, len(SLASH_COMMANDS))
    assert any("registered" in r.getMessage() for r in caplog.records)


async def test_unchanged_commands_are_not_sent_again(tmp_path):
    http = FakeHTTP(current=as_discord_returns(command_payload()))
    await sync_channel(tmp_path, http).sync_commands()
    assert [c[0] for c in http.calls] == ["GET"]  # no PUT: nothing changed


async def test_a_failed_registration_is_one_warning_not_fatal(tmp_path, caplog):
    http = FakeHTTP(fail=RuntimeError("403 Forbidden"))
    await sync_channel(tmp_path, http).sync_commands()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "typed commands still work" in warnings[0].getMessage()


# ---- P30 fix round 1: an answer that hangs, an interaction that cannot be read, a message during the answer ----

async def test_an_answer_that_hangs_holds_back_nothing_for_long(tmp_path, monkeypatch, caplog):
    import octopus.channels.discord as discord_channel

    monkeypatch.setattr(discord_channel, "ACK_TIMEOUT_SECONDS", 0.05)
    caplog.set_level(logging.INFO, logger="octopus.channels.discord")
    ch = channel(tmp_path)
    got = []

    async def on_message(m):
        got.append(m.callback or m.text)

    stuck = tap("cmd:status")
    stuck.response.gate = asyncio.Event()  # never set: a half-open connection
    await asyncio.wait_for(ch.handle_callback(stuck, on_message), 1)
    await ch.handle_message(message("后面的字"), on_message)
    await asyncio.wait_for(ch.drain(), 1)
    assert got == ["cmd:status", "后面的字"]
    assert any("could not answer a tap" in r.getMessage() and "TimeoutError" in r.getMessage()
               for r in caplog.records)


async def test_the_ack_timeout_is_past_discords_3_seconds_but_short():
    from octopus.channels.discord import ACK_TIMEOUT_SECONDS

    assert 3 < ACK_TIMEOUT_SECONDS <= 10


async def test_an_interaction_that_cannot_be_read_holds_back_nothing(tmp_path, monkeypatch, caplog):
    ch = channel(tmp_path)
    got = []

    async def on_message(m):
        got.append(m.text)

    async def broken(interaction):
        raise RuntimeError("bad interaction")

    monkeypatch.setattr(ch, "on_callback", broken)
    await ch.handle_callback(tap("cmd:status"), on_message)
    await ch.handle_message(message("后面的字"), on_message)
    await ch.drain()
    assert got == ["后面的字"]
    assert any("could not read an interaction" in r.getMessage() for r in caplog.records)


async def test_a_message_sent_while_the_answer_is_on_its_way_waits_for_the_tap(tmp_path):
    ch = channel(tmp_path)
    got = []

    async def on_message(m):
        got.append(m.callback or m.text)

    t = tap("cmd:status")
    t.response.gate = asyncio.Event()
    answering = asyncio.create_task(ch.handle_callback(t, on_message))
    await asyncio.sleep(0.01)
    assert t.response.calls == [("defer", None)]  # on its way
    await ch.handle_message(message("后面的字"), on_message)
    await asyncio.sleep(0.05)
    assert got == []
    t.response.gate.set()
    await answering
    await ch.drain()
    assert got == ["cmd:status", "后面的字"]


async def test_a_failed_registration_is_tried_again_on_the_next_ready(tmp_path):
    http = FakeHTTP(fail=RuntimeError("503"))
    ch = sync_channel(tmp_path, http)
    ch._commands_synced = True  # as on_ready sets it before the check runs
    await ch.sync_commands()
    assert ch._commands_synced is False
