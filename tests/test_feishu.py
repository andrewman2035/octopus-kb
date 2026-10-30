import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from octopus.channels.base import Button, Reply
from octopus.channels import feishu
from octopus.channels.feishu import MAX_TEXT, FeishuChannel, card_json


class FakeApi:
    def __init__(self):
        self.texts, self.cards, self.images = [], [], []

    async def send_text(self, open_id, text):
        self.texts.append((open_id, text))
        return f"om_{len(self.texts)}"

    async def send_card(self, open_id, markdown, buttons, plain=False):
        self.cards.append((open_id, markdown, buttons))
        return "om_card"

    async def send_image(self, open_id, path):
        self.images.append((open_id, path))
        return "om_img"

    async def download(self, message_id, key, kind):
        return f"{kind}:{key}".encode()


def event(message_type, content, chat_type="p2p", parent_id=None, message_id="om_in"):
    msg = SimpleNamespace(message_id=message_id, chat_type=chat_type, message_type=message_type,
                          content=json.dumps(content, ensure_ascii=False), parent_id=parent_id)
    sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_me"), sender_type="user")
    return SimpleNamespace(event=SimpleNamespace(message=msg, sender=sender))


async def test_text(tmp_path):
    m = await FeishuChannel("a", "s", tmp_path, api=FakeApi()).to_incoming(event("text", {"text": "你好"}, parent_id="om_p"))
    assert (m.channel, m.user_id, m.text, m.reply_to_id, m.message_id) == ("feishu", "ou_me", "你好", "om_p", "om_in")


async def test_image_and_file_downloaded(tmp_path):
    ch = FeishuChannel("a", "s", tmp_path, api=FakeApi())
    img = await ch.to_incoming(event("image", {"image_key": "img_1"}, message_id="om_1"))
    assert img.attachments[0].kind == "image" and img.attachments[0].path.read_bytes() == b"image:img_1"
    f = await ch.to_incoming(event("file", {"file_key": "file_1", "file_name": "报告.pdf"}, message_id="om_2"))
    assert f.attachments[0].filename == "报告.pdf" and f.attachments[0].path.read_bytes() == b"file:file_1"
    a = await ch.to_incoming(event("audio", {"file_key": "file_2", "duration": 3000}, message_id="om_3"))
    assert a.attachments[0].kind == "audio" and a.attachments[0].voice is True  # hold-to-talk recording
    assert f.attachments[0].voice is False and img.attachments[0].voice is False
    v = await ch.to_incoming(event("media", {"file_key": "file_3", "image_key": "img_c", "file_name": "v.mp4"},
                                   message_id="om_4"))
    assert v.attachments[0].kind == "video" and v.attachments[0].filename == "v.mp4" and not v.attachments[0].voice


async def test_hostile_filenames_stay_inside_incoming_dir(tmp_path):
    ch = FeishuChannel("a", "s", tmp_path / "incoming", api=FakeApi())
    for i, name in enumerate(["../../evil.sh", "/etc/passwd", "..", ".", ""]):
        m = await ch.to_incoming(event("file", {"file_key": "k", "file_name": name}, message_id=f"om_{i}"))
        p = m.attachments[0].path
        assert p.parent.parent == tmp_path / "incoming" and p.read_bytes() == b"file:k"
        assert p.name not in ("", ".", "..") and m.attachments[0].filename == p.name
    assert not (tmp_path / "evil.sh").exists()


async def test_post_text_and_images(tmp_path):
    content = {"title": "标题", "content": [[{"tag": "text", "text": "第一行"}, {"tag": "a", "text": "链接", "href": "https://a.com"}],
                                          [{"tag": "img", "image_key": "img_9"}]]}
    m = await FeishuChannel("a", "s", tmp_path, api=FakeApi()).to_incoming(event("post", content))
    assert m.text == "标题\n第一行链接 https://a.com"
    assert m.attachments[0].kind == "image"


async def test_unsupported_and_empty_messages_ignored(tmp_path):
    ch = FeishuChannel("a", "s", tmp_path, api=FakeApi())
    assert await ch.to_incoming(event("sticker", {"file_key": "x"}, message_id="om_1")) is None
    assert await ch.to_incoming(event("text", {"text": "  "}, message_id="om_2")) is None


async def test_group_and_bot_messages_ignored(tmp_path):
    ch = FeishuChannel("a", "s", tmp_path, api=FakeApi())
    assert await ch.to_incoming(event("text", {"text": "x"}, chat_type="group")) is None
    e = event("text", {"text": "x"})
    e.event.sender.sender_type = "app"
    assert await ch.to_incoming(e) is None


async def test_duplicate_events_dropped(tmp_path):
    ch = FeishuChannel("a", "s", tmp_path, api=FakeApi())
    assert await ch.to_incoming(event("text", {"text": "x"}, message_id="om_1")) is not None
    assert await ch.to_incoming(event("text", {"text": "x"}, message_id="om_1")) is None


async def test_send_text_and_card(tmp_path):
    api = FakeApi()
    ch = FeishuChannel("a", "s", tmp_path, api=api)
    r = await ch.send("ou_me", Reply("你好"))
    assert r.ok and r.message_id == "om_1" and api.texts == [("ou_me", "你好")]
    r = await ch.send("ou_me", Reply("已保存", [Button("改成提问", "fix:3", "回复“问”改成提问")]))
    assert r.message_id == "om_card" and api.cards[0][2][0].data == "fix:3"


async def test_long_text_is_chunked_and_card_goes_last(tmp_path):
    api = FakeApi()
    ch = FeishuChannel("a", "s", tmp_path, api=api)
    r = await ch.send("ou_me", Reply("a" * (MAX_TEXT + 5), [Button("x", "fix:1", "x")]))
    assert r.ok and len(api.texts) == 1 and len(api.texts[0][1]) == MAX_TEXT and len(api.cards) == 1


def test_card_json_has_callback_buttons():
    card = card_json("**已保存**", [Button("改成提问", "fix:3", "x")])
    text = json.dumps(card, ensure_ascii=False)
    assert "改成提问" in text and '"fix": "fix:3"' in text


def test_card_action_to_incoming(tmp_path):
    ev = SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value={"fix": "fix:3"}),
                                               operator=SimpleNamespace(open_id="ou_me")))
    m = FeishuChannel("a", "s", tmp_path, api=FakeApi()).card_to_incoming(ev)
    assert (m.user_id, m.callback) == ("ou_me", "fix:3")
    other = SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value={"x": 1}),
                                                  operator=SimpleNamespace(open_id="ou_me")))
    assert FeishuChannel("a", "s", tmp_path, api=FakeApi()).card_to_incoming(other) is None


async def test_send_failure(tmp_path):
    class Broken(FakeApi):
        async def send_text(self, open_id, text):
            raise RuntimeError("boom")

    assert (await FeishuChannel("a", "s", tmp_path, api=Broken()).send("ou_me", Reply("x"))).ok is False


async def test_partial_multi_chunk_failure_still_ok(tmp_path):
    class SecondFails(FakeApi):
        async def send_text(self, open_id, text):
            if self.texts:
                raise RuntimeError("boom")
            return await super().send_text(open_id, text)

    ch = FeishuChannel("a", "s", tmp_path, api=SecondFails())
    r = await ch.send("ou_me", Reply("a" * (MAX_TEXT + 5)))
    assert r.ok and r.message_id == "om_1"


async def test_send_image(tmp_path):
    api = FakeApi()
    ch = FeishuChannel("a", "s", tmp_path, api=api)
    r = await ch.send_image("ou_me", tmp_path / "a.png", caption="图")
    assert r.ok and r.message_id == "om_img" and api.texts == [("ou_me", "图")] and api.images[0][0] == "ou_me"

    class Broken(FakeApi):
        async def send_image(self, open_id, path):
            raise RuntimeError("boom")

    assert (await FeishuChannel("a", "s", tmp_path, api=Broken()).send_image("ou_me", tmp_path / "a.png")).ok is False


FAILED = "附件下载失败，请重发一次。"


async def test_disallowed_user_ignored_before_download_or_reply(tmp_path):
    class Spy(FakeApi):
        downloads = 0

        async def download(self, message_id, key, kind):
            Spy.downloads += 1
            return b"x"

    api = Spy()
    ch = FeishuChannel("a", "s", tmp_path, api=api, allowed=lambda uid: uid == "ou_other")
    assert await ch.to_incoming(event("image", {"image_key": "k"})) is None
    assert await ch.to_incoming(event("text", {"text": "hi"}, message_id="om_2")) is None
    ev = SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value={"fix": "fix:3"}),
                                               operator=SimpleNamespace(open_id="ou_me")))
    assert ch.card_to_incoming(ev) is None
    assert Spy.downloads == 0 and api.texts == [] and not list(tmp_path.iterdir())
    ok = FeishuChannel("a", "s", tmp_path, api=FakeApi(), allowed=lambda uid: uid == "ou_me")
    assert (await ok.to_incoming(event("text", {"text": "hi"}))).text == "hi"


async def test_allow_check_error_rejects(tmp_path):
    def boom(uid):
        raise RuntimeError("x")

    ch = FeishuChannel("a", "s", tmp_path, api=FakeApi(), allowed=boom)
    assert await ch.to_incoming(event("text", {"text": "hi"})) is None


async def test_oversized_download_discarded_with_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu, "MAX_DOWNLOAD", 5)

    class Big(FakeApi):
        async def download(self, message_id, key, kind):
            return b"123456"

    api = Big()
    ch = FeishuChannel("a", "s", tmp_path, api=api)
    assert await ch.to_incoming(event("file", {"file_key": "k", "file_name": "a.bin"})) is None
    assert api.texts == [("ou_me", feishu.TOO_BIG)] and "50MB" in feishu.TOO_BIG
    assert not list(tmp_path.iterdir())


async def test_partial_post_failure_cleans_staged_files_and_replies(tmp_path):
    class Flaky(FakeApi):
        async def download(self, message_id, key, kind):
            if key == "img_2":
                raise RuntimeError("net")
            return b"ok"

    content = {"content": [[{"tag": "img", "image_key": "img_1"}], [{"tag": "img", "image_key": "img_2"}]]}
    api = Flaky()
    ch = FeishuChannel("a", "s", tmp_path, api=api)
    assert await ch.to_incoming(event("post", content)) is None
    assert api.texts == [("ou_me", FAILED)]
    assert list(tmp_path.rglob("*")) == []


async def test_write_failure_removes_its_dir_and_earlier_ones(tmp_path, monkeypatch):
    content = {"content": [[{"tag": "img", "image_key": "img_1"}], [{"tag": "img", "image_key": "img_2"}]]}
    real, calls = feishu.Path.write_bytes, []

    def flaky_write(self, data):
        calls.append(self)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(self, data)

    monkeypatch.setattr(feishu.Path, "write_bytes", flaky_write)
    api = FakeApi()
    assert await FeishuChannel("a", "s", tmp_path, api=api).to_incoming(event("post", content)) is None
    assert api.texts == [("ou_me", FAILED)] and list(tmp_path.rglob("*")) == []


async def test_card_failure_falls_back_to_text_with_hints(tmp_path):
    class NoCards(FakeApi):
        async def send_card(self, open_id, markdown, buttons, plain=False):
            raise RuntimeError("card rejected")

    api = NoCards()
    r = await FeishuChannel("a", "s", tmp_path, api=api).send(
        "ou_me", Reply("已保存", [Button("改成提问", "fix:3", "回复“问”改成提问")]))
    assert r.ok and r.message_id == "om_1"
    assert api.texts == [("ou_me", "已保存\n回复“问”改成提问")]


def test_card_clicks_deduped_for_ten_seconds(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(feishu.time, "monotonic", lambda: now[0])
    ch = FeishuChannel("a", "s", tmp_path, api=FakeApi())

    def click(value="fix:3", who="ou_me"):
        return ch.card_to_incoming(SimpleNamespace(event=SimpleNamespace(
            action=SimpleNamespace(value={"fix": value}), operator=SimpleNamespace(open_id=who))))

    assert click() is not None and click() is None
    assert click("fix:4") is not None and click(who="ou_x") is not None
    now[0] += 11
    assert click() is not None


@pytest.mark.parametrize("connected,misses,expected", [
    (True, 2, (0, False)), (False, 0, (1, False)), (False, 1, (2, False)), (False, 2, (3, True)),
])
def test_watchdog_step(connected, misses, expected):
    assert feishu.watchdog_step(connected, misses) == expected


async def test_refuses_second_ws_thread_while_first_alive(tmp_path):
    ch = FeishuChannel("a", "s", tmp_path, api=FakeApi())
    ch._ws_thread = SimpleNamespace(is_alive=lambda: True)

    async def on_message(m):
        pass

    with pytest.raises(RuntimeError, match="飞书长连接还在运行"):
        await ch.start(on_message)


def test_shutdown_ws_disconnects_and_stops_loop():
    ws_loop = asyncio.new_event_loop()
    t = threading.Thread(target=ws_loop.run_forever)
    t.start()

    class FakeClient:
        disconnected = False

        async def _disconnect(self):
            FakeClient.disconnected = True

    feishu.shutdown_ws(FakeClient(), ws_loop, timeout=2)
    t.join(2)
    assert FakeClient.disconnected and not t.is_alive()
    ws_loop.close()


def test_shutdown_ws_survives_hung_disconnect():
    ws_loop = asyncio.new_event_loop()
    t = threading.Thread(target=ws_loop.run_forever)
    t.start()

    class Hung:
        async def _disconnect(self):
            await asyncio.sleep(60)

    feishu.shutdown_ws(Hung(), ws_loop, timeout=0.2)
    t.join(2)
    assert not t.is_alive()
    ws_loop.run_until_complete(asyncio.sleep(0))  # let the cancellation land
    ws_loop.close()


def test_shutdown_ws_disables_auto_reconnect_before_stopping():
    ws_loop = asyncio.new_event_loop()
    t = threading.Thread(target=ws_loop.run_forever)
    t.start()
    seen = []

    class FakeClient:
        _auto_reconnect = True

        async def _disconnect(self):
            seen.append(self._auto_reconnect)

    client = FakeClient()
    feishu.shutdown_ws(client, ws_loop, timeout=2)
    t.join(2)
    assert client._auto_reconnect is False and seen == [False]
    ws_loop.close()


# ---- final review M1 ----

async def test_filenames_use_shared_safe_filename(tmp_path):
    ch = FeishuChannel("a", "s", tmp_path / "incoming", api=FakeApi())
    m = await ch.to_incoming(event("file", {"file_key": "k", "file_name": "a\x00b\n" + "长" * 100 + ".pdf"},
                                   message_id="om_long"))
    name = m.attachments[0].filename
    assert len(name.encode("utf-8")) <= 200 and name.startswith("ab长") and name.endswith(".pdf")
    m = await ch.to_incoming(event("file", {"file_key": "k", "file_name": "..\\..\\w.pdf"}, message_id="om_win"))
    assert m.attachments[0].filename == "w.pdf"


async def test_messages_dropped_before_the_app_are_logged_with_their_reply(tmp_path, monkeypatch, caplog):
    import logging

    from octopus.channels.base import user_tag

    tag = user_tag("ou_me")
    caplog.set_level(logging.INFO)

    class Failing(FakeApi):
        async def download(self, message_id, key, kind):
            raise RuntimeError("cdn down")

    ch = FeishuChannel("a", "s", tmp_path, api=Failing())
    assert await ch.to_incoming(event("file", {"file_key": "k"}, message_id="om_1")) is None
    assert await ch.to_incoming(event("sticker", {"file_key": "x"}, message_id="om_2")) is None
    monkeypatch.setattr(feishu, "MAX_DOWNLOAD", 5)

    class Big(FakeApi):
        async def download(self, message_id, key, kind):
            return b"123456"

    ch = FeishuChannel("a", "s", tmp_path, api=Big())
    assert await ch.to_incoming(event("file", {"file_key": "k"}, message_id="om_3")) is None
    text = caplog.text
    for reason in ("download failed", "nothing readable (sticker)", "too big"):
        assert f"in feishu {tag} not handed over: {reason}" in text
    assert f"out feishu {tag} " in text and "ou_me" not in text


async def test_a_stop_mid_post_removes_the_files_already_staged(tmp_path):
    # P19 (F11 on 飞书): the second image's download is cancelled by a stop; the first one's dir goes too
    class Stopped(FakeApi):
        async def download(self, message_id, key, kind):
            if key == "img_2":
                raise asyncio.CancelledError
            return b"ok"

    content = {"content": [[{"tag": "img", "image_key": "img_1"}], [{"tag": "img", "image_key": "img_2"}]]}
    ch = FeishuChannel("a", "s", tmp_path, api=Stopped())
    with pytest.raises(asyncio.CancelledError):
        await ch.to_incoming(event("post", content))
    assert list(tmp_path.rglob("*")) == []
