import asyncio
import itertools
import json
import logging

import pytest

from octopus.channels import clawbot
from octopus.channels.base import Button, Reply, user_tag
from octopus.channels.clawbot import (MAX_TEXT, RELOGIN_TEXT, SEND_BUDGET, VOICE_NO_TEXT, ClawBotChannel,
                                      login_interactive)
from octopus.channels.ilink import STALE_TOKEN, ILinkError, LoginResult
from octopus.models import Attachment


class MemorySecrets:
    def __init__(self, token="tok"):
        self.values = {"clawbot_token": token} if token else {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


class FakeClient:
    instances = []

    def __init__(self, token=None, base_url=None, http=None):
        self.token, self.base_url = token, base_url
        self.sent, self.updates, self.fail_send = [], [], None
        self.media = {}
        FakeClient.instances.append(self)

    async def send_text(self, to, text, ctx):
        if self.fail_send:
            raise self.fail_send
        self.sent.append((to, text, ctx))
        return f"mid{len(self.sent)}"

    async def get_updates(self, buf):
        if self.updates:
            item = self.updates.pop(0)
            if callable(item):
                item = item()
            if isinstance(item, Exception):
                raise item
            return item
        raise KeyboardInterrupt  # ends the test loop

    async def download_media(self, media, aeskey_hex=None):
        return self.media.get(media.get("encrypt_query_param"), b"bytes")

    async def get_qrcode(self):
        return "q1", "https://weixin.qq.com/qr"

    async def login(self, qrcode, on_status=None, verify_code_fn=None, timeout=480):
        return LoginResult("newtok", "https://ilinkai.weixin.qq.com", "b@im.bot", "owner@im.wechat")

    async def close(self):
        pass


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def make(tmp_path, token="tok", owner="owner@im.wechat", notify=None, clock=None):
    FakeClient.instances = []
    state = tmp_path / "clawbot-state.json"
    state.write_text(json.dumps({"get_updates_buf": "", "context_tokens": {}, "owner_user_id": owner}), encoding="utf-8")
    ch = ClawBotChannel(MemorySecrets(token), state, tmp_path / "incoming", tmp_path / "qr.png",
                        client_factory=FakeClient, notify=notify, clock=clock or Clock())
    return ch


_IDS = itertools.count(1000)


def text_msg(text, frm="owner@im.wechat", ctx="ctx1", mtype=1, mid=None):
    """Each message its own id unless one is given (the channel skips an id it has handed over before)."""
    return {"message_id": mid if mid is not None else next(_IDS), "from_user_id": frm, "message_type": mtype, "context_token": ctx,
            "item_list": [{"type": 1, "text_item": {"text": text}}]}


async def test_owner_and_mapping(tmp_path):
    ch = make(tmp_path)
    assert ch.is_owner("owner@im.wechat") and not ch.is_owner("x@im.wechat")
    m = await ch.to_incoming(text_msg("你好"))
    assert (m.channel, m.user_id, m.text) == ("clawbot", "owner@im.wechat", "你好")
    assert json.loads((tmp_path / "clawbot-state.json").read_text())["context_tokens"]["owner@im.wechat"] == "ctx1"
    assert await ch.to_incoming(text_msg("echo", mtype=2)) is None  # bot's own messages


def voice_msg(transcript, frm="owner@im.wechat"):
    return {"message_id": 2, "from_user_id": frm, "message_type": 1, "context_token": "c",
            "item_list": [{"type": 3, "voice_item": {"text": transcript, "media": {}}}]}


async def test_voice_becomes_audio_attachment_with_wechat_transcript(tmp_path):
    ch = make(tmp_path)
    m = await ch.to_incoming(voice_msg(" 语音转好的文字 "))
    assert m.text == ""
    assert m.attachments == [Attachment(kind="audio", path=None, filename="voice.silk", text="语音转好的文字",
                                        voice=True)]


async def test_voice_without_transcript_is_not_saved_and_user_is_told(tmp_path):
    ch = make(tmp_path)
    ch._client = FakeClient("tok")
    assert await ch.to_incoming(voice_msg("")) is None
    await ch._drain()
    assert VOICE_NO_TEXT == "这条语音没有识别出文字，请重发或者打字。"
    assert ch._client.sent == [("owner@im.wechat", VOICE_NO_TEXT, "c")]


async def test_image_and_file_downloaded(tmp_path):
    ch = make(tmp_path)
    ch._client = FakeClient("tok")
    ch._client.media = {"img": b"PNG", "f": b"PDF"}
    msg = {"message_id": 3, "from_user_id": "owner@im.wechat", "message_type": 1, "context_token": "c",
           "item_list": [{"type": 2, "image_item": {"aeskey": "00" * 16, "media": {"encrypt_query_param": "img"}}},
                         {"type": 4, "file_item": {"file_name": "报告.pdf", "media": {"encrypt_query_param": "f"}}}]}
    m = await ch.to_incoming(msg)
    kinds = [(a.kind, a.filename, a.path.read_bytes()) for a in m.attachments]
    assert kinds == [("image", "image.jpg", b"PNG"), ("file", "报告.pdf", b"PDF")]


async def test_send_uses_context_token_splits_and_hints(tmp_path):
    ch = make(tmp_path)
    await ch.to_incoming(text_msg("hi", ctx="ctxA"))
    ch._client = FakeClient("tok")
    r = await ch.send("owner@im.wechat", Reply("x" * (MAX_TEXT + 5), [Button("改成提问", "fix:1", "回复“问”改成提问")]))
    assert r.ok and r.message_id == "mid2"
    assert [len(t) for _, t, _ in ch._client.sent] == [MAX_TEXT, 5 + len("\n回复“问”改成提问")]
    assert ch._client.sent[-1][1].endswith("回复“问”改成提问")
    assert all(c == "ctxA" for _, _, c in ch._client.sent)


async def test_send_without_context_token_fails(tmp_path):
    ch = make(tmp_path)
    ch._client = FakeClient("tok")
    assert (await ch.send("owner@im.wechat", Reply("x"))).ok is False


async def test_rate_limit_budget(tmp_path):
    clock = Clock()
    ch = make(tmp_path, clock=clock)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    results = [(await ch.send("owner@im.wechat", Reply(f"{i}"))).ok for i in range(SEND_BUDGET + 1)]
    assert results == [True] * SEND_BUDGET + [False]
    clock.now += 481
    assert (await ch.send("owner@im.wechat", Reply("later"))).ok is True


async def test_fake_success_and_errors_are_failures(tmp_path):
    ch = make(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")

    async def no_id(to, text, ctx):
        return None

    ch._client.send_text = no_id
    assert (await ch.send("owner@im.wechat", Reply("x"))).ok is False
    ch._client = FakeClient("tok")
    ch._client.fail_send = ILinkError(-2, "rate limited")
    assert (await ch.send("owner@im.wechat", Reply("x"))).ok is False


async def test_stale_token_triggers_relogin_notice(tmp_path):
    notices = []

    async def notify(text, image):
        notices.append((text, image))
        return 1

    ch = make(tmp_path, notify=notify)
    got = []

    async def on_message(m):
        got.append(m)

    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([text_msg("hi")], "buf2", 1.0), ILinkError(STALE_TOKEN, "expired")]
    try:
        await ch.start(on_message)
    except KeyboardInterrupt:
        pass
    assert [m.text for m in got] == ["hi"]
    assert notices and notices[0][0] == RELOGIN_TEXT and notices[0][1] == tmp_path / "qr.png"
    assert (tmp_path / "qr.png").exists()
    assert ch.secrets.get("clawbot_token") == "newtok"  # re-login completed and stored
    assert json.loads((tmp_path / "clawbot-state.json").read_text())["get_updates_buf"] == "buf2"


async def test_login_interactive_saves_token_and_owner(tmp_path):
    secrets = MemorySecrets(token=None)
    out = []
    opened = []
    res = await login_interactive(secrets, tmp_path / "state.json", tmp_path / "qr.png", client_factory=FakeClient,
                                  print_fn=out.append, input_fn=lambda p: "", open_fn=opened.append)
    assert opened == [tmp_path / "qr.png"]
    assert res.user_id == "owner@im.wechat" and secrets.get("clawbot_token") == "newtok"
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["owner_user_id"] == "owner@im.wechat"
    assert (tmp_path / "qr.png").exists()
    assert any("扫码" in line for line in out)


# ---- rulings: partial delivery, budget accounting, file safety, token hygiene, re-login retries ----

@pytest.fixture(autouse=True)
def pauses(monkeypatch):  # every test: no real waiting (e.g. the pause after -14)
    calls = []

    async def fake(seconds):
        calls.append(seconds)

    monkeypatch.setattr(clawbot, "_pause", fake)
    return calls


async def test_budget_exhausted_mid_reply_still_ok_without_resending(tmp_path):
    ch = make(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    long = "\n".join(["y" * MAX_TEXT] * (SEND_BUDGET + 1))
    r = await ch.send("owner@im.wechat", Reply(long))
    assert r.ok and r.message_id == f"mid{SEND_BUDGET}"
    assert len(ch._client.sent) == SEND_BUDGET
    assert (await ch.send("owner@im.wechat", Reply("more"))).ok is False  # budget gone, nothing sent
    assert len(ch._client.sent) == SEND_BUDGET


async def test_error_or_fake_success_on_later_chunk_is_ok(tmp_path):
    ch = make(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    for failure in (ILinkError(-2, "rate limited"), None):
        ch._client = FakeClient("tok")
        real = ch._client.send_text
        calls = []

        async def flaky(to, text, ctx, real=real, calls=calls, failure=failure):
            calls.append(text)
            if len(calls) == 2:
                if failure:
                    raise failure
                return None
            return await real(to, text, ctx)

        ch._client.send_text = flaky
        ch._sent.clear()
        r = await ch.send("owner@im.wechat", Reply("a" * MAX_TEXT + "\n" + "b" * MAX_TEXT + "\nc"))
        assert r.ok and r.message_id == "mid1"
        assert len(calls) == 2  # stopped after the failed chunk; the delivered first chunk is not re-sent


async def test_errors_do_not_use_budget(tmp_path):
    ch = make(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    ch._client.fail_send = ILinkError(-2, "rate limited")
    for _ in range(SEND_BUDGET + 2):
        assert (await ch.send("owner@im.wechat", Reply("x"))).ok is False
    ch._client.fail_send = None
    assert [(await ch.send("owner@im.wechat", Reply(f"{i}"))).ok for i in range(SEND_BUDGET)] == [True] * SEND_BUDGET


async def test_state_file_is_private(tmp_path):
    ch = make(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    assert (tmp_path / "clawbot-state.json").stat().st_mode & 0o777 == 0o600


async def test_media_filenames_are_bare_and_staged_per_message(tmp_path):
    ch = make(tmp_path)
    ch._client = FakeClient("tok")
    msg = {"message_id": 4, "from_user_id": "owner@im.wechat", "message_type": 1, "context_token": "c",
           "item_list": [{"type": 4, "file_item": {"file_name": "../../evil.txt", "media": {}}},
                         {"type": 4, "file_item": {"file_name": "..\\..\\win.txt", "media": {}}},
                         {"type": 4, "file_item": {"file_name": "..", "media": {}}},
                         {"type": 5, "video_item": {"media": {}}}]}
    m = await ch.to_incoming(msg)
    incoming = (tmp_path / "incoming").resolve()
    assert [a.filename for a in m.attachments] == ["evil.txt", "win.txt", "file", "video.mp4"]
    assert [a.kind for a in m.attachments] == ["file", "file", "file", "video"]
    for a in m.attachments:
        assert a.path.resolve().parent.parent == incoming
    assert len({a.path.parent for a in m.attachments}) == len(m.attachments)


async def test_media_download_failure_keeps_text_and_says_so(tmp_path):
    ch = make(tmp_path)
    ch._client = FakeClient("tok")

    async def broken(media, aeskey_hex=None):
        raise ILinkError(None, "网络错误")

    ch._client.download_media = broken
    msg = {"message_id": 5, "from_user_id": "owner@im.wechat", "message_type": 1, "context_token": "c",
           "item_list": [{"type": 1, "text_item": {"text": "看图"}},
                         {"type": 2, "image_item": {"media": {"encrypt_query_param": "img"}}}]}
    m = await ch.to_incoming(msg)
    assert m.attachments == [] and "看图" in m.text and "附件下载失败" in m.text


async def test_relogin_never_logs_or_persists_token(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    ch = make(tmp_path)
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [ILinkError(STALE_TOKEN, "expired")]

    async def on_message(m):
        pass

    with pytest.raises(KeyboardInterrupt):
        await ch.start(on_message)
    assert ch.secrets.get("clawbot_token") == "newtok"
    assert "newtok" not in caplog.text
    state_text = (tmp_path / "clawbot-state.json").read_text()
    assert "newtok" not in state_text
    state = json.loads(state_text)
    assert (state["owner_user_id"], state["bot_id"], state["base_url"]) == (
        "owner@im.wechat", "b@im.bot", "https://ilinkai.weixin.qq.com")
    assert FakeClient.instances[-1].token == "newtok"


async def test_failed_relogin_retries_with_fresh_qr(tmp_path, pauses):
    notices = []

    async def notify(text, image):
        notices.append(text)
        return 1

    attempts = []

    class Flaky(FakeClient):
        async def login(self, qrcode, on_status=None, verify_code_fn=None, timeout=480):
            attempts.append(qrcode)
            if len(attempts) == 1:
                raise ILinkError(None, "登录失败：expired")
            return await super().login(qrcode)

    ch = make(tmp_path, token=None, notify=notify)
    ch.client_factory = Flaky

    async def on_message(m):
        pass

    with pytest.raises(KeyboardInterrupt):
        await ch.start(on_message)
    assert len(attempts) == 2 and notices == [RELOGIN_TEXT, RELOGIN_TEXT]  # one notice per fresh QR
    assert pauses == [900]
    assert ch.secrets.get("clawbot_token") == "newtok"


async def test_new_bot_account_resets_cursor_and_contexts(tmp_path):
    ch = make(tmp_path)
    (tmp_path / "clawbot-state.json").write_text(json.dumps(
        {"bot_id": "old@im.bot", "get_updates_buf": "oldbuf", "context_tokens": {"owner@im.wechat": "c"},
         "owner_user_id": "owner@im.wechat"}), encoding="utf-8")
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [ILinkError(STALE_TOKEN, "expired")]

    async def on_message(m):
        pass

    with pytest.raises(KeyboardInterrupt):
        await ch.start(on_message)
    state = json.loads((tmp_path / "clawbot-state.json").read_text())
    assert (state["bot_id"], state["get_updates_buf"], state["context_tokens"]) == ("b@im.bot", "", {})


async def test_login_interactive_prints_each_status_once(tmp_path):
    class Chatty(FakeClient):
        async def login(self, qrcode, on_status=None, verify_code_fn=None, timeout=480):
            for s in ("wait", "wait", "scaned", "scaned", "confirmed"):
                on_status(s)
            return await super().login(qrcode)

    out = []
    await login_interactive(MemorySecrets(None), tmp_path / "s.json", tmp_path / "qr.png", client_factory=Chatty,
                            print_fn=out.append, input_fn=lambda p: "", open_fn=lambda p: None)
    statuses = [line for line in out if line.startswith("状态")]
    assert len(statuses) == 3
    assert not any("newtok" in line for line in out)
    assert (tmp_path / "s.json").stat().st_mode & 0o777 == 0o600


async def test_failed_write_removes_staging_dir(tmp_path):
    ch = make(tmp_path)
    ch._client = FakeClient("tok")
    ch._client.media = {"img": "not bytes"}  # write_bytes raises TypeError
    msg = {"message_id": 6, "from_user_id": "owner@im.wechat", "message_type": 1, "context_token": "c",
           "item_list": [{"type": 2, "image_item": {"media": {"encrypt_query_param": "img"}}}]}
    m = await ch.to_incoming(msg)
    assert m.text == "（附件下载失败）" and m.attachments == []
    assert list((tmp_path / "incoming").iterdir()) == []


# ---- fix round 1 ----

def _noop_factory():
    async def on_message(m):
        pass
    return on_message


async def test_cli_login_mid_run_is_picked_up_and_its_token_kept(tmp_path):
    notices = []

    async def notify(text, image):
        notices.append(text)
        return 1

    ch = make(tmp_path, notify=notify)
    state_path = tmp_path / "clawbot-state.json"

    def cli_login():  # `octopus clawbot-login` in another process while the service runs
        ch.secrets.set("clawbot_token", "tok2")
        state = json.loads(state_path.read_text())
        state.update({"owner_user_id": "new@im.wechat", "bot_id": "b2@im.bot", "base_url": "https://new.example.com"})
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return [text_msg("after", ctx="ctx9")], "buf3", 1.0

    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([text_msg("hi")], "buf2", 1.0), cli_login, ILinkError(STALE_TOKEN, "expired")]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    state = json.loads(state_path.read_text())
    assert (state["owner_user_id"], state["bot_id"], state["base_url"]) == (
        "new@im.wechat", "b2@im.bot", "https://new.example.com")  # the service's saves did not clobber them
    assert state["get_updates_buf"] == "buf3" and state["context_tokens"]["owner@im.wechat"] == "ctx9"
    assert ch.secrets.get("clawbot_token") == "tok2"  # the old session's -14 did not delete the new token
    assert notices == []  # no QR re-login: it just reconnected
    assert (FakeClient.instances[-1].token, FakeClient.instances[-1].base_url) == ("tok2", "https://new.example.com")
    assert ch.is_owner("new@im.wechat") and not ch.is_owner("owner@im.wechat")


async def test_stale_token_deletes_only_the_session_token(tmp_path):
    deleted = []
    ch = make(tmp_path)
    real_delete = ch.secrets.delete
    ch.secrets.delete = lambda name: (deleted.append((name, ch.secrets.get(name))), real_delete(name))
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [ILinkError(STALE_TOKEN, "expired")]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    assert deleted == [("clawbot_token", "tok")]  # once, by the -14 path; relogin() itself deletes nothing
    assert ch.secrets.get("clawbot_token") == "newtok"


async def test_locked_keychain_starts_relogin_without_deleting(tmp_path):
    class Locked(MemorySecrets):
        def __init__(self):
            super().__init__("tok")
            self.locked, self.deleted = True, []

        def get(self, name):
            return None if self.locked else super().get(name)

        def set(self, name, value):
            self.locked = False
            super().set(name, value)

        def delete(self, name):
            self.deleted.append(name)
            super().delete(name)

    ch = make(tmp_path)
    ch.secrets = Locked()
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    assert ch.secrets.deleted == [] and ch.secrets.get("clawbot_token") == "newtok"


async def test_poll_errors_back_off_and_reset_on_success(tmp_path, pauses):
    ch = make(tmp_path)
    got = []

    async def on_message(m):
        got.append(m.text)

    err = ILinkError(-1, "busy")
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [err] * 7 + [([text_msg("ok")], "b1", 1.0), err]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(on_message)
    assert pauses == [2, 4, 8, 16, 32, 60, 60, 2]
    assert got == ["ok"]


async def test_empty_instant_polls_are_paced(tmp_path, pauses):
    ch = make(tmp_path)
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([], "", 35.0), ([], "", 35.0)]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    assert len(pauses) == 2 and all(0 < p <= 1.0 for p in pauses)


async def test_bad_message_is_skipped_and_loop_continues(tmp_path):
    ch = make(tmp_path)
    got = []

    async def on_message(m):
        got.append(m.text)

    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [(["not a dict", text_msg("one")], "b1", 1.0), ([text_msg("two")], "b2", 1.0)]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(on_message)
    assert got == ["one", "two"]


async def test_each_relogin_attempt_sends_one_qr_and_notice_on_a_schedule(tmp_path, pauses):
    notices, qrs, attempts = [], [], []

    async def notify(text, image):
        notices.append((text, image.stat().st_mode & 0o777))
        return 1

    class Flaky(FakeClient):
        async def get_qrcode(self):
            qrs.append(1)
            return await super().get_qrcode()

        async def login(self, qrcode, on_status=None, verify_code_fn=None, timeout=480):
            attempts.append(1)
            if len(attempts) < 7:
                raise ILinkError(None, "登录失败：expired")
            return await super().login(qrcode)

    ch = make(tmp_path, token=None, notify=notify)
    ch.client_factory = Flaky
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    assert pauses == [900, 1800, 3600, 7200, 10800, 10800]  # last step repeats
    assert len(qrs) == len(attempts) == len(notices) == 7  # one QR per notice, none unseen
    assert all(n == (RELOGIN_TEXT, 0o600) for n in notices)
    assert ch.secrets.get("clawbot_token") == "newtok"


async def test_relogin_schedule_restarts_after_success(tmp_path, pauses):
    attempts = []

    class Flaky(FakeClient):
        def __init__(self, token=None, base_url=None, http=None):
            super().__init__(token, base_url, http)
            if token and len(attempts) == 2:  # the session after the first successful login expires at once
                self.updates = [ILinkError(STALE_TOKEN, "expired")]

        async def login(self, qrcode, on_status=None, verify_code_fn=None, timeout=480):
            attempts.append(1)
            if len(attempts) in (1, 3):
                raise ILinkError(None, "登录失败：expired")
            return await super().login(qrcode)

    ch = make(tmp_path, token=None)
    ch.client_factory = Flaky
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    assert len(attempts) == 4 and pauses == [900, clawbot.STALE_PAUSE, 900]  # final review I5: pause after -14


async def test_undelivered_notice_is_logged_with_cli_hint(tmp_path, caplog):
    async def notify(text, image):
        return 0

    expected = (f"微信 ClawBot 需要重新扫码：二维码在 {tmp_path / 'qr.png'}（有效约 8 分钟）；"
                "或者先停止 Octopus，再运行 octopus clawbot-login。")
    for n in (notify, None):
        caplog.clear()
        ch = make(tmp_path, token=None, notify=n)
        with pytest.raises(KeyboardInterrupt):
            await ch.start(_noop_factory())
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert expected in warnings


async def test_keychain_write_failure_keeps_login_for_this_session(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    class ReadOnly(MemorySecrets):
        def set(self, name, value):
            raise RuntimeError("keychain locked")

    ch = make(tmp_path)
    ch.secrets = ReadOnly("tok")
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [ILinkError(STALE_TOKEN, "expired")]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    assert FakeClient.instances[-1].token == "newtok"  # reconnected with the token kept in memory
    assert any(r.levelno == logging.ERROR and "Keychain" in r.getMessage() for r in caplog.records)
    assert "newtok" not in caplog.text
    assert json.loads((tmp_path / "clawbot-state.json").read_text())["bot_id"] == "b@im.bot"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_persist_keeps_login_fields_when_state_file_is_lost(tmp_path, damage):
    ch = make(tmp_path)
    ch.state.update({"bot_id": "b@im.bot", "base_url": "https://x.example.com"})
    path = tmp_path / "clawbot-state.json"
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("{not json", encoding="utf-8")
    await ch.to_incoming(text_msg("hi", ctx="ctxZ"))
    state = json.loads(path.read_text())
    assert (state["owner_user_id"], state["bot_id"], state["base_url"]) == (
        "owner@im.wechat", "b@im.bot", "https://x.example.com")
    assert state["context_tokens"]["owner@im.wechat"] == "ctxZ"


async def test_qr_png_is_private(tmp_path):
    await login_interactive(MemorySecrets(None), tmp_path / "s.json", tmp_path / "qr.png", client_factory=FakeClient,
                            print_fn=lambda s: None, input_fn=lambda p: "", open_fn=lambda p: None)
    qr = tmp_path / "qr.png"
    assert qr.stat().st_mode & 0o777 == 0o600 and qr.read_bytes().startswith(b"\x89PNG")
    ch = make(tmp_path, token=None)
    qr.chmod(0o644)
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    assert qr.stat().st_mode & 0o777 == 0o600


async def test_long_filenames_truncated_keeping_suffix(tmp_path):
    ch = make(tmp_path)
    ch._client = FakeClient("tok")
    long = "长" * 100 + ".pdf"
    msg = {"message_id": 7, "from_user_id": "owner@im.wechat", "message_type": 1, "context_token": "c",
           "item_list": [{"type": 4, "file_item": {"file_name": long, "media": {}}}]}
    a = (await ch.to_incoming(msg)).attachments[0]
    assert len(a.filename.encode("utf-8")) <= 200 and a.filename.endswith(".pdf") and a.filename.startswith("长")
    assert a.path.name == a.filename


async def test_shutdown_drains_with_timeout_then_cancels(tmp_path, monkeypatch):
    monkeypatch.setattr(clawbot, "DRAIN_TIMEOUT", 0.05)
    ch = make(tmp_path)
    outcome = []

    async def on_message(m):
        if m.text == "fast":
            outcome.append("fast done")
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            outcome.append("slow cancelled")
            raise

    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([text_msg("fast"), text_msg("slow")], "b1", 1.0)]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(on_message)
    assert sorted(outcome) == ["fast done", "slow cancelled"]
    assert not ch._tasks


# ---- final review ----

async def test_reply_that_does_not_fit_the_remaining_budget_is_held_whole(tmp_path):
    ch = make(tmp_path)
    await ch.to_incoming(text_msg("hi"))
    ch._client = FakeClient("tok")
    assert (await ch.send("owner@im.wechat", Reply("first"))).ok
    three = "\n".join([c * MAX_TEXT for c in "abc"][:SEND_BUDGET])
    r = await ch.send("owner@im.wechat", Reply(three))
    assert r.ok is False  # App keeps the whole text in the outbox
    assert [t for _, t, _ in ch._client.sent] == ["first"]  # nothing of it was sent
    two = "\n".join(["z" * MAX_TEXT] * (SEND_BUDGET - 1))
    assert (await ch.send("owner@im.wechat", Reply(two))).ok
    assert len(ch._client.sent) == SEND_BUDGET


async def test_stale_token_left_by_a_failed_keychain_delete_is_not_reused(tmp_path, pauses):
    class Sticky(MemorySecrets):
        def delete(self, name):  # the Keychain delete failed silently
            pass

    stale_sessions = []

    class Stale(FakeClient):
        def __init__(self, token=None, base_url=None, http=None):
            super().__init__(token, base_url, http)
            if token == "tok":
                stale_sessions.append(1)
                if len(stale_sessions) < 5:  # bounded so the unfixed loop ends too
                    self.updates = [ILinkError(STALE_TOKEN, "expired")]

    ch = make(tmp_path)
    ch.secrets = Sticky("tok")
    ch.client_factory = Stale
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    assert len(stale_sessions) == 1  # never reconnected with the token that just expired
    assert FakeClient.instances[-1].token == "newtok"  # a QR re-login happened instead
    assert clawbot.STALE_PAUSE >= 5 and pauses[0] == clawbot.STALE_PAUSE  # paused before reconnecting


async def test_messages_from_strangers_leave_no_trace(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    ch = make(tmp_path)
    ch._client = FakeClient("tok")
    downloads = []

    async def download(media, aeskey_hex=None):
        downloads.append(media)
        return b"x"

    ch._client.download_media = download
    msg = {"message_id": 8, "from_user_id": "stranger@im.wechat", "message_type": 1, "context_token": "evil",
           "item_list": [{"type": 1, "text_item": {"text": "hi"}},
                         {"type": 2, "image_item": {"media": {"encrypt_query_param": "img"}}}]}
    assert await ch.to_incoming(msg) is None
    assert downloads == [] and not (tmp_path / "incoming").exists()
    assert "stranger@im.wechat" not in ch.state["context_tokens"]
    assert not (tmp_path / "clawbot-state.json").read_text().count("evil")
    assert any(r.levelno == logging.INFO and "stranger@im.wechat" in r.getMessage() for r in caplog.records)
    ch.allowed = lambda uid: uid == "friend@im.wechat"  # allow-listed in config: accepted like the owner
    assert (await ch.to_incoming(text_msg("hi", frm="friend@im.wechat"))).user_id == "friend@im.wechat"


# ---- P4 Task 5: /clawbot asks for a fresh QR; Keychain calls off the event loop ----

class Done(Exception):
    """Ends start() run as a task (a KeyboardInterrupt inside a task would escape the event loop)."""


class TaskClient(FakeClient):
    async def get_updates(self, buf):
        raise Done


async def _until(cond, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


async def test_request_login_while_connected_does_nothing(tmp_path):
    ch = make(tmp_path)
    ch._client = FakeClient("tok")
    assert ch.connected is True
    assert ch.request_login() is False


async def test_request_login_cuts_the_schedule_wait_short(tmp_path, monkeypatch):
    waits, notices, attempts = [], [], []

    async def long_pause(seconds):
        waits.append(seconds)
        await asyncio.sleep(3600)

    monkeypatch.setattr(clawbot, "_pause", long_pause)

    async def notify(text, image):
        notices.append(text)
        return 1

    class Flaky(TaskClient):
        async def login(self, qrcode, on_status=None, verify_code_fn=None, timeout=480):
            attempts.append(1)
            if len(attempts) == 1:
                raise ILinkError(None, "登录失败：expired")
            return await super().login(qrcode)

    ch = make(tmp_path, token=None, notify=notify)
    ch.client_factory = Flaky
    task = asyncio.create_task(ch.start(_noop_factory()))
    await _until(lambda: waits == [900])  # the first QR went unscanned: waiting 15 min for the next
    assert ch.connected is False
    assert ch.request_login() is True
    with pytest.raises(Done):
        await asyncio.wait_for(task, 3)
    assert len(attempts) == 2 and notices == [RELOGIN_TEXT, RELOGIN_TEXT]
    assert ch.secrets.get("clawbot_token") == "newtok"


async def test_request_login_during_an_attempt_starts_a_fresh_qr(tmp_path, pauses):
    notices, qrs, cancelled = [], [], []

    async def notify(text, image):
        notices.append(text)
        return 1

    class Slow(TaskClient):
        async def get_qrcode(self):
            qrs.append(1)
            return f"q{len(qrs)}", "https://weixin.qq.com/qr"

        async def login(self, qrcode, on_status=None, verify_code_fn=None, timeout=480):
            if qrcode == "q1":  # the owner lost this one: it would wait out its ~8 minutes
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancelled.append(qrcode)
                    raise
            return await super().login(qrcode)

    ch = make(tmp_path, token=None, notify=notify)
    ch.client_factory = Slow
    task = asyncio.create_task(ch.start(_noop_factory()))
    await _until(lambda: len(notices) == 1)
    assert ch.request_login() is True
    with pytest.raises(Done):
        await asyncio.wait_for(task, 3)
    assert len(qrs) == 2 and notices == [RELOGIN_TEXT, RELOGIN_TEXT] and cancelled == ["q1"]
    assert pauses == []  # no schedule wait in between
    assert ch.secrets.get("clawbot_token") == "newtok"


async def test_keychain_calls_run_off_the_event_loop(tmp_path):
    import threading

    loop_thread = threading.get_ident()
    calls = []

    class Recording(MemorySecrets):
        def get(self, name):
            calls.append(("get", threading.get_ident()))
            return super().get(name)

        def set(self, name, value):
            calls.append(("set", threading.get_ident()))
            super().set(name, value)

        def delete(self, name):
            calls.append(("delete", threading.get_ident()))
            super().delete(name)

    ch = make(tmp_path)
    ch.secrets = Recording("tok")
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [ILinkError(STALE_TOKEN, "expired")]  # stale check + delete, relogin set, connect
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    assert {name for name, _ in calls} == {"get", "set", "delete"}
    assert all(tid != loop_thread for _, tid in calls), calls


async def test_keychain_delete_error_after_stale_token_does_not_stop_the_channel(tmp_path, pauses):
    class DeleteDenied(MemorySecrets):
        def delete(self, name):
            raise RuntimeError("denied")

    ch = make(tmp_path)
    ch.secrets = DeleteDenied("tok")
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [ILinkError(STALE_TOKEN, "expired")]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    assert FakeClient.instances[-1].token == "newtok"  # the dead token was not reused: a QR re-login instead


async def test_buttons_without_fallback_add_no_text(tmp_path):
    ch = make(tmp_path)
    await ch.to_incoming(text_msg("hi", ctx="ctxA"))
    ch._client = FakeClient("tok")
    await ch.send("owner@im.wechat", Reply("说明", [Button("最近存的", "cmd:recent", ""), Button("运行状态", "cmd:status", "")]))
    assert ch._client.sent[-1][1] == "说明"
    await ch.send("owner@im.wechat", Reply("列表", [Button("删除 a", "del:1", ""), Button("删除 b", "del:2", "用法")]))
    assert ch._client.sent[-1][1] == "列表\n用法"


async def test_cursor_moves_only_after_the_batch_is_handed_over(tmp_path):
    """P16: a stop (cancel) while a batch is still being read must not lose the rest of it: the old cursor is kept,
    so the server sends the batch again."""
    ch = make(tmp_path)
    got = []

    async def on_message(m):
        got.append(m.text)

    async def stopped(media, aeskey_hex=None):
        raise asyncio.CancelledError

    image = {"message_id": 5, "from_user_id": "owner@im.wechat", "message_type": 1, "context_token": "c",
             "item_list": [{"type": 2, "image_item": {"media": {"encrypt_query_param": "img"}}}]}
    ch._client_override = FakeClient("tok")
    ch._client_override.download_media = stopped
    ch._client_override.updates = [([image, text_msg("存")], "b1", 1.0)]
    with pytest.raises(asyncio.CancelledError):
        await ch.start(on_message)
    assert got == [] and json.loads((tmp_path / "clawbot-state.json").read_text())["get_updates_buf"] == ""


async def test_updates_are_counted_and_a_message_with_nothing_readable_is_logged(tmp_path, caplog):
    ch = make(tmp_path)
    got = []

    async def on_message(m):
        got.append(m.text)

    odd = {"message_id": 6, "from_user_id": "owner@im.wechat", "message_type": 1, "context_token": "c",
           "item_list": [{"type": 9}]}
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([text_msg("一"), odd], "b1", 1.0)]
    with caplog.at_level(logging.DEBUG), pytest.raises(KeyboardInterrupt):
        await ch.start(on_message)
    assert got == ["一"]
    assert "clawbot: 2 update(s)" in caplog.text
    assert f"in clawbot {user_tag('owner@im.wechat')} not handed over: nothing readable (item types [9])" in caplog.text
    assert "owner@im.wechat" not in caplog.text
    assert json.loads((tmp_path / "clawbot-state.json").read_text())["get_updates_buf"] == "b1"


async def test_a_batch_replayed_after_a_stop_is_handed_over_once(tmp_path, caplog):
    """F3: the server sends the batch again from the old cursor; what was handed over before is skipped, by id,
    also across a restart (the ids are in the state file)."""
    got = []

    async def on_message(m):
        got.append(m.text or m.attachments[0].kind)

    image = {"message_id": 25, "from_user_id": "owner@im.wechat", "message_type": 1, "context_token": "c",
             "item_list": [{"type": 2, "image_item": {"media": {"encrypt_query_param": "img"}}}]}
    batch = [text_msg("/s 笔记", mid=21), text_msg("存", mid=22), text_msg("复利是什么", mid=23),
             text_msg("确认", mid=24), image]

    async def stopped(media, aeskey_hex=None):
        raise asyncio.CancelledError

    ch = make(tmp_path)
    ch._client_override = FakeClient("tok")
    ch._client_override.download_media = stopped
    ch._client_override.updates = [(batch, "b1", 1.0)]
    with pytest.raises(asyncio.CancelledError):
        await ch.start(on_message)
    assert got == ["/s 笔记", "存", "复利是什么", "确认"]
    state = tmp_path / "clawbot-state.json"
    assert json.loads(state.read_text())["get_updates_buf"] == ""

    again = ClawBotChannel(MemorySecrets("tok"), state, tmp_path / "incoming", tmp_path / "qr.png",
                           client_factory=FakeClient, clock=Clock())
    again._client_override = FakeClient("tok")
    again._client_override.updates = [(batch, "b1", 1.0), (batch, "b1", 1.0)]  # and once more, same process
    with caplog.at_level(logging.DEBUG, logger="octopus.channels.clawbot"), pytest.raises(KeyboardInterrupt):
        await again.start(on_message)
    assert got == ["/s 笔记", "存", "复利是什么", "确认", "image"]
    assert "clawbot: message 21 handed over before, skipped" in caplog.text
    assert json.loads(state.read_text())["get_updates_buf"] == "b1"


async def test_recent_ids_are_capped_and_reset_for_another_bot(tmp_path):
    ch = make(tmp_path)
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([text_msg(str(i), mid=i) for i in range(clawbot.RECENT_IDS + 5)], "b1", 1.0)]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    ids = json.loads((tmp_path / "clawbot-state.json").read_text())["recent_ids"]
    assert len(ids) == clawbot.RECENT_IDS and ids[-1] == str(clawbot.RECENT_IDS + 4)
    state = {"bot_id": "old@im.bot", "recent_ids": ["1"], "get_updates_buf": "x", "context_tokens": {}}
    clawbot._apply_login(state, LoginResult("t", "https://ilinkai.weixin.qq.com", "new@im.bot", "o"))
    assert state["recent_ids"] == []


async def test_send_results_say_why_nothing_went_out(tmp_path):
    ch = make(tmp_path)
    assert (await ch.send("owner@im.wechat", Reply("x"))).reason == "not-connected"
    ch._client = FakeClient("tok")
    assert (await ch.send("owner@im.wechat", Reply("x"))).reason == "no-context"
    await ch.to_incoming(text_msg("hi"))
    for _ in range(SEND_BUDGET):
        assert (await ch.send("owner@im.wechat", Reply("x"))).ok
    assert (await ch.send("owner@im.wechat", Reply("x"))).reason == "budget"
    (tmp_path / "o").mkdir()
    other = make(tmp_path / "o")
    await other.to_incoming(text_msg("hi"))
    other._client = FakeClient("tok")
    other._client.fail_send = ILinkError(-2, "rate limited")
    assert (await other.send("owner@im.wechat", Reply("x"))).reason == "error"


# ---- P19: a message cut short by a stop is handled after the restart, once ----

def _again(tmp_path, wall=None):
    """The same channel after a restart: a new object reading the state file the first one left."""
    ch = ClawBotChannel(MemorySecrets("tok"), tmp_path / "clawbot-state.json", tmp_path / "incoming",
                        tmp_path / "qr.png", client_factory=FakeClient, clock=Clock(), wall=wall or (lambda: 100.0))
    ch._client_override = FakeClient("tok")
    return ch


def _inflight(tmp_path):
    return [i["id"] for i in json.loads((tmp_path / "clawbot-state.json").read_text()).get("inflight", [])]


async def _cut_short(tmp_path, monkeypatch, batch):
    """Run the batch; the message "slow" is still being handled when the channel stops (drain timeout)."""
    monkeypatch.setattr(clawbot, "DRAIN_TIMEOUT", 0.05)
    got, on_disk = [], []

    async def on_message(m):
        if m.text == "slow":
            on_disk.append(_inflight(tmp_path))  # what a SIGKILL right now would leave
            await asyncio.Event().wait()
        got.append(m.text)

    ch = make(tmp_path)
    ch.wall = lambda: 100.0
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [(batch, "b1", 1.0)]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(on_message)
    return got, on_disk


async def test_a_message_cut_short_by_a_stop_is_handled_after_the_restart_once(tmp_path, monkeypatch, caplog):
    batch = [text_msg("fast", mid=41), text_msg("slow", mid=42)]
    got, on_disk = await _cut_short(tmp_path, monkeypatch, batch)
    assert got == ["fast"] and "42" in on_disk[0]
    assert _inflight(tmp_path) == ["42"]  # accepted, not done

    again = _again(tmp_path, wall=lambda: 100.0 + 60)
    again._client_override.updates = [(batch, "b1", 1.0)]  # the server sends the batch again too
    seen = []

    async def on_message(m):
        seen.append((m.text, m.message_id))

    with caplog.at_level(logging.INFO, logger="octopus.channels.clawbot"), pytest.raises(KeyboardInterrupt):
        await again.start(on_message)
    assert seen == [("slow", "42")]
    assert _inflight(tmp_path) == []
    assert "clawbot: message 42 was cut short by a stop; handling it again" in caplog.text


async def test_a_message_cut_short_long_ago_is_dropped_with_a_log_line(tmp_path, monkeypatch, caplog):
    await _cut_short(tmp_path, monkeypatch, [text_msg("slow", mid=43)])
    again = _again(tmp_path, wall=lambda: 100.0 + clawbot.REPLAY_MAX_AGE + 1)
    seen = []

    async def on_message(m):
        seen.append(m.text)

    with caplog.at_level(logging.INFO, logger="octopus.channels.clawbot"), pytest.raises(KeyboardInterrupt):
        await again.start(on_message)
    assert seen == [] and _inflight(tmp_path) == []
    assert "clawbot: message 43 came 30 min ago; too old to handle now, dropped" in caplog.text


async def test_a_message_cut_short_keeps_its_files_for_the_restart(tmp_path, monkeypatch):
    image = {"message_id": 44, "from_user_id": "owner@im.wechat", "message_type": 1, "context_token": "c",
             "item_list": [{"type": 1, "text_item": {"text": "slow"}},
                           {"type": 2, "image_item": {"media": {"encrypt_query_param": "img"}}}]}
    await _cut_short(tmp_path, monkeypatch, [image])
    seen = []

    async def on_message(m):
        seen.append((m.text, [(a.kind, a.path.read_bytes()) for a in m.attachments]))

    again = _again(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        await again.start(on_message)
    assert seen == [("slow", [("image", b"bytes")])]


async def test_a_message_whose_handling_failed_is_not_handled_again(tmp_path, caplog):
    async def on_message(m):
        raise RuntimeError("bug")

    ch = make(tmp_path)
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([text_msg("x", mid=45)], "b1", 1.0)]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(on_message)
    assert _inflight(tmp_path) == []
    assert "clawbot: handling message 45 failed" in caplog.text


# ---- P19: a state file that cannot be saved (disk full) does not stop the channel ----

async def test_a_state_save_that_fails_is_logged_once_and_tried_again(tmp_path, monkeypatch, caplog):
    real, fails = clawbot._save_state, [3]

    def flaky(path, state):
        if fails[0]:
            fails[0] -= 1
            raise OSError(28, "No space left on device")
        real(path, state)

    monkeypatch.setattr(clawbot, "_save_state", flaky)
    got = []

    async def on_message(m):
        got.append(m.text)

    ch = make(tmp_path)
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([text_msg("一", mid=51), text_msg("二", mid=52)], "b1", 1.0),
                                   ([text_msg("三", mid=53)], "b2", 1.0)]
    with caplog.at_level(logging.INFO, logger="octopus.channels.clawbot"), pytest.raises(KeyboardInterrupt):
        await ch.start(on_message)
    assert got == ["一", "二", "三"]
    assert caplog.text.count("clawbot: could not save the state file") == 1
    assert "clawbot: the state file is saved again" in caplog.text
    state = json.loads((tmp_path / "clawbot-state.json").read_text())
    assert state["recent_ids"][-3:] == ["51", "52", "53"] and state["get_updates_buf"] == "b2"


async def test_files_staged_for_a_message_whose_next_download_is_cancelled_are_removed(tmp_path):
    # F11: the first image was staged, the second download cancelled by a stop: nothing is left in incoming
    ch = make(tmp_path)
    ch._client = FakeClient("tok")
    calls = []

    async def second_cancelled(media, aeskey_hex=None):
        calls.append(media)
        if len(calls) == 2:
            raise asyncio.CancelledError
        return b"bytes"

    ch._client.download_media = second_cancelled
    msg = {"message_id": 61, "from_user_id": "owner@im.wechat", "message_type": 1, "context_token": "c",
           "item_list": [{"type": 2, "image_item": {"media": {"encrypt_query_param": "a"}}},
                         {"type": 2, "image_item": {"media": {"encrypt_query_param": "b"}}}]}
    with pytest.raises(asyncio.CancelledError):
        await ch.to_incoming(msg)
    assert len(calls) == 2
    assert list((tmp_path / "incoming").iterdir()) == []


async def test_a_relogin_whose_state_save_fails_keeps_running(tmp_path, monkeypatch, caplog):
    # P3 deferred minor: the OSError escaped start() (the supervisor restarted the channel, the login fields were lost)
    def full(path, state):
        raise OSError(28, "No space left on device")

    ch = make(tmp_path)
    monkeypatch.setattr(clawbot, "_save_state", full)
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([text_msg("x", mid=71)], "b1", 1.0), ILinkError(STALE_TOKEN, "expired")]
    with pytest.raises(KeyboardInterrupt):  # the new session's poll ends the test, not the OSError
        await ch.start(_noop_factory())
    assert FakeClient.instances[-1].token == "newtok"
    assert "71" in ch.state["recent_ids"] and ch.state["get_updates_buf"] == "b1"  # what the file never got
    assert (ch.state["bot_id"], ch.state["owner_user_id"]) == ("b@im.bot", "owner@im.wechat")
    assert caplog.text.count("clawbot: could not save the state file") == 1


# ---- P19 fix round 1 ----

async def test_a_replayed_message_is_answered_at_once(tmp_path, monkeypatch):
    # M2: the replay runs once the client is connected, so a quick reply is not held in the outbox
    await _cut_short(tmp_path, monkeypatch, [text_msg("slow", mid=91)])
    again = _again(tmp_path)
    again._client_override = None  # connects as in real use: the Keychain read lets other tasks run first
    results = []

    async def on_message(m):
        results.append(await again.send(m.user_id, Reply("好")))

    with pytest.raises(KeyboardInterrupt):
        await again.start(on_message)
    assert [r.ok for r in results] == [True]


async def test_a_message_cut_short_on_every_replay_is_dropped_after_two(tmp_path, monkeypatch, caplog):
    # M3: never handled again on every start (each try may pay for a model call)
    await _cut_short(tmp_path, monkeypatch, [text_msg("slow", mid=92)])
    tries = []

    async def hangs(m):
        tries.append(m.message_id)
        await asyncio.Event().wait()

    for _ in range(3):
        again = _again(tmp_path)
        with caplog.at_level(logging.INFO, logger="octopus.channels.clawbot"), pytest.raises(KeyboardInterrupt):
            await again.start(hangs)
    assert tries == ["92", "92"]
    assert _inflight(tmp_path) == []
    assert "clawbot: message 92 was handled again 2 times and cut short each time; dropped" in caplog.text


async def test_inflight_messages_too_old_are_dropped_when_the_file_is_read(tmp_path, caplog):
    # M8: their text does not stay in the state file until ClawBot happens to start again
    state = tmp_path / "clawbot-state.json"
    state.write_text(json.dumps({"owner_user_id": "owner@im.wechat", "inflight": [
        {"id": "93", "at": 0.0, "msg": {"user_id": "owner@im.wechat", "text": "旧的"}},
        {"id": "94", "at": 9_000.0, "msg": {"user_id": "owner@im.wechat", "text": "新的"}}]}), encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="octopus.channels.clawbot"):
        ClawBotChannel(MemorySecrets("tok"), state, tmp_path / "incoming", tmp_path / "qr.png",
                       client_factory=FakeClient, clock=Clock(), wall=lambda: 10_000.0)
    assert _inflight(tmp_path) == ["94"]
    assert "旧的" not in state.read_text(encoding="utf-8")
    assert "clawbot: message 93 came 166 min ago; too old to handle now, dropped" in caplog.text


async def test_inflight_is_capped(tmp_path, monkeypatch):
    # M8: a hung App does not grow the state file with every message's text
    monkeypatch.setattr(clawbot, "DRAIN_TIMEOUT", 0.05)

    async def hangs(m):
        await asyncio.Event().wait()

    ch = make(tmp_path)
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([text_msg(str(i), mid=100 + i) for i in range(clawbot.INFLIGHT_MAX + 5)],
                                    "b1", 1.0)]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(hangs)
    # integration (P19 review R1): the head of the user's queue is the one App is handling: it stays
    assert _inflight(tmp_path) == ["100"] + [str(100 + i) for i in range(6, clawbot.INFLIGHT_MAX + 5)]


async def test_the_inflight_cap_warns_once_per_streak(tmp_path, monkeypatch, caplog):
    # P19 review R1: one warning while the count stays over the cap, not one per message
    monkeypatch.setattr(clawbot, "DRAIN_TIMEOUT", 0.05)

    async def hangs(m):
        await asyncio.Event().wait()

    ch = make(tmp_path)
    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([text_msg(str(i), mid=200 + i) for i in range(clawbot.INFLIGHT_MAX + 5)],
                                    "b1", 1.0)]
    with caplog.at_level(logging.WARNING, logger="octopus.channels.clawbot"), pytest.raises(KeyboardInterrupt):
        await ch.start(hangs)
    assert caplog.text.count("in flight") == 1


async def test_a_message_still_being_handled_is_not_logged_as_dropped(tmp_path, monkeypatch, caplog):
    # P19 review R2: past REPLAY_MAX_AGE its entry goes (no replay), but its handler is still running
    monkeypatch.setattr(clawbot, "DRAIN_TIMEOUT", 0.05)
    wall = [0.0]
    state = tmp_path / "clawbot-state.json"
    state.write_text(json.dumps({"owner_user_id": "owner@im.wechat"}), encoding="utf-8")
    ch = ClawBotChannel(MemorySecrets("tok"), state, tmp_path / "incoming", tmp_path / "qr.png",
                        client_factory=FakeClient, clock=Clock(), wall=lambda: wall[0])

    async def hangs(m):
        await asyncio.Event().wait()

    def later():
        wall[0] = clawbot.REPLAY_MAX_AGE + 60
        return [text_msg("新的", mid=302)], "b2", 1.0

    ch._client_override = FakeClient("tok")
    ch._client_override.updates = [([text_msg("慢的", mid=301)], "b1", 1.0), later]
    with caplog.at_level(logging.INFO, logger="octopus.channels.clawbot"), pytest.raises(KeyboardInterrupt):
        await ch.start(hangs)
    assert "message 301" in caplog.text and "still being handled" in caplog.text
    assert "too old to handle now, dropped" not in caplog.text
    assert _inflight(tmp_path) == ["302"]


async def test_a_relogin_kept_only_in_memory_reaches_the_file_once_saving_works(tmp_path, monkeypatch):
    # M1: later saves wrote the cursor but never the new login (and none at all for another bot account)
    state = tmp_path / "clawbot-state.json"
    full, real = [False], clawbot._save_state

    def flaky(path, data):
        if full[0]:
            raise OSError(28, "No space left on device")
        real(path, data)

    class Client(FakeClient):
        def __init__(self, token=None, base_url=None, http=None):
            super().__init__(token, base_url, http)
            if token == "newtok":
                self.updates = [([text_msg("x", mid=95)], "b1", 1.0)]

    monkeypatch.setattr(clawbot, "_save_state", flaky)
    state.write_text(json.dumps({"owner_user_id": "owner@im.wechat", "bot_id": "old@im.bot",
                                 "base_url": "https://old.example"}), encoding="utf-8")
    ch = ClawBotChannel(MemorySecrets("tok"), state, tmp_path / "incoming", tmp_path / "qr.png",
                        client_factory=Client, clock=Clock())
    relogin = ch.relogin

    async def relogin_on_a_full_disk():
        full[0] = True
        try:
            await relogin()
        finally:
            full[0] = False

    monkeypatch.setattr(ch, "relogin", relogin_on_a_full_disk)
    ch._client_override = Client("tok")
    ch._client_override.updates = [ILinkError(STALE_TOKEN, "expired")]
    with pytest.raises(KeyboardInterrupt):
        await ch.start(_noop_factory())
    disk = json.loads(state.read_text())
    assert (disk["bot_id"], disk["owner_user_id"], disk["base_url"]) == (
        "b@im.bot", "owner@im.wechat", "https://ilinkai.weixin.qq.com")
    assert "95" in disk["recent_ids"]
