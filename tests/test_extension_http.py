"""P17: the local save endpoint (127.0.0.1 only, token, Host check, CORS for the extension only, size cap)."""
import asyncio
import json
import logging
import socket

import pytest

import octopus.channels.extension as ext_mod
from octopus.channels.base import Reply
from octopus.channels.extension import ExtensionChannel

TOKEN = "s3cret-token-value-that-is-long-enough-000"
EXT = "chrome-extension://abcdefghijklmnop"


class FakeIntake:
    def __init__(self):
        self.saved, self.replies = [], []

    async def save(self, req):
        self.saved.append(req)
        return len(self.saved)

    async def recent(self):
        return {"telegram": False, "items": [{"id": 1, "state": "已存"}]}

    async def reply(self, user_id, text):
        self.replies.append((user_id, text))


@pytest.fixture
async def server():
    ch = ExtensionChannel(TOKEN, port=0)
    ch.intake = FakeIntake()
    task = asyncio.create_task(ch.start(None))
    await asyncio.wait_for(ch.ready.wait(), 5)
    yield ch
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def call(ch, method, path, headers=None, body=b"", host=None, raw_head=None):
    """One raw HTTP/1.1 request: (status, headers, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", ch.port)
    h = {"Host": host if host is not None else f"127.0.0.1:{ch.port}"}
    if body or method == "POST":
        h["Content-Length"] = str(len(body))
    h.update(headers or {})
    head = raw_head or (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items() if v is not None)
                        + "\r\n")
    writer.write(head.encode("latin-1") + body)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    top, _, payload = data.partition(b"\r\n\r\n")
    lines = top.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    got = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return status, got, payload


def auth(**more):
    return {"Authorization": f"Bearer {TOKEN}", **more}


def save_body(**fields):
    return json.dumps({"kind": "selection", "url": "https://a.com/x", "title": "t", "text": "选中的字", **fields}).encode()


async def test_binds_to_127_0_0_1_only(server):
    assert server.server.sockets[0].getsockname()[0] == "127.0.0.1"
    assert ext_mod.HOST == "127.0.0.1" and ext_mod.DEFAULT_PORT == 8765


async def test_recent_with_the_token(server):
    status, headers, body = await call(server, "GET", "/recent", auth())
    assert status == 200 and json.loads(body) == await server.intake.recent()
    assert headers["content-type"].startswith("application/json") and headers["cache-control"] == "no-store"


async def test_save_returns_its_id_at_once(server):
    status, _, body = await call(server, "POST", "/save", auth(**{"Content-Type": "application/json"}), save_body())
    assert status == 202 and json.loads(body) == {"id": 1}
    [req] = server.intake.saved
    assert (req.kind, req.url, req.text) == ("selection", "https://a.com/x", "选中的字")


@pytest.mark.parametrize("value", [None, "", f"Bearer {TOKEN}x", f"bearer {TOKEN}", TOKEN, "Bearer "])
async def test_a_missing_or_wrong_token_is_401_with_no_detail(server, value, caplog):
    caplog.set_level(logging.DEBUG)
    status, _, body = await call(server, "POST", "/save", {"Authorization": value}, save_body())
    assert status == 401 and body == b""
    status, _, body = await call(server, "GET", "/recent", {"Authorization": value})
    assert status == 401 and body == b""
    assert server.intake.saved == [] and TOKEN not in caplog.text


@pytest.mark.parametrize("host", ["evil.example:{port}", "127.0.0.1:1", "127.0.0.1", "", "localhost.evil:{port}"])
async def test_a_foreign_host_header_is_refused(server, host):
    status, _, _ = await call(server, "GET", "/recent", auth(), host=host.format(port=server.port))
    assert status == 403


async def test_a_missing_host_header_is_refused(server):
    head = f"GET /recent HTTP/1.1\r\nAuthorization: Bearer {TOKEN}\r\n\r\n"
    status, _, _ = await call(server, "GET", "/recent", raw_head=head)
    assert status == 403


async def test_localhost_is_a_good_host(server):
    status, _, _ = await call(server, "GET", "/recent", auth(), host=f"localhost:{server.port}")
    assert status == 200


async def test_preflight_is_answered_for_the_extension_only(server):
    asked = {"Origin": EXT, "Access-Control-Request-Method": "POST",
             "Access-Control-Request-Headers": "authorization,content-type",
             "Access-Control-Request-Private-Network": "true"}
    status, headers, body = await call(server, "OPTIONS", "/save", asked)
    assert status == 204 and body == b""
    assert headers["access-control-allow-origin"] == EXT and headers["vary"] == "Origin"
    assert "authorization" in headers["access-control-allow-headers"].lower()
    assert "POST" in headers["access-control-allow-methods"]
    assert headers["access-control-allow-private-network"] == "true"
    status, headers, _ = await call(server, "OPTIONS", "/save", {**asked, "Origin": "https://evil.example"})
    assert status == 403 and "access-control-allow-origin" not in headers


async def test_a_web_page_cannot_save_even_with_the_token(server):
    status, headers, _ = await call(server, "POST", "/save", auth(Origin="https://evil.example"), save_body())
    assert status == 403 and "access-control-allow-origin" not in headers
    assert server.intake.saved == []


async def test_the_extension_can_read_every_answer(server):
    status, headers, _ = await call(server, "POST", "/save", {"Origin": EXT}, save_body())
    assert status == 401 and headers["access-control-allow-origin"] == EXT  # so the popup can say 密码不对
    status, headers, _ = await call(server, "POST", "/save", auth(Origin=EXT), save_body())
    assert status == 202 and headers["access-control-allow-origin"] == EXT


async def test_a_body_over_the_cap_is_413(server, monkeypatch):
    monkeypatch.setattr(ext_mod, "MAX_BODY_BYTES", 100)
    status, _, _ = await call(server, "POST", "/save", auth(Origin=EXT), save_body(text="字" * 200))
    assert status == 413 and server.intake.saved == []
    assert ext_mod.MAX_BODY_BYTES == 100


def test_the_real_cap_is_15_mb():
    assert ext_mod.MAX_BODY_BYTES == 15 * 1024 * 1024


@pytest.mark.parametrize("method,path,expected", [("GET", "/", 404), ("GET", "/save", 405), ("POST", "/recent", 405),
                                                  ("GET", "/recent/../save", 404), ("DELETE", "/save", 405)])
async def test_only_the_two_routes(server, method, path, expected):
    status, _, _ = await call(server, method, path, auth(), b"{}" if method == "POST" else b"")
    assert status == expected


async def test_bad_json_and_bad_requests_are_400_with_a_reason(server):
    status, _, body = await call(server, "POST", "/save", auth(), b"{not json")
    assert status == 400 and json.loads(body)["error"]
    status, _, body = await call(server, "POST", "/save", auth(), save_body(text=""))
    assert status == 400 and json.loads(body) == {"error": "没有选中文字"}


async def test_post_needs_a_length(server):
    head = (f"POST /save HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nAuthorization: Bearer {TOKEN}\r\n"
            "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
    status, _, _ = await call(server, "POST", "/save", raw_head=head)
    assert status == 411


async def test_a_header_flood_is_refused(server):
    head = f"GET /recent HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nX: {'a' * 70000}\r\n\r\n"
    status, _, _ = await call(server, "GET", "/recent", raw_head=head)
    assert status == 431


async def test_a_save_that_breaks_is_500_and_the_server_goes_on(server):
    async def boom(req):
        raise RuntimeError("bug")

    server.intake.save = boom
    status, _, body = await call(server, "POST", "/save", auth(), save_body())
    assert status == 500 and json.loads(body) == {"error": "保存出错了"}
    assert (await call(server, "GET", "/recent", auth()))[0] == 200


async def test_send_hands_the_reply_to_the_intake():
    ch = ExtensionChannel(TOKEN, port=0)
    ch.intake = FakeIntake()
    result = await ch.send("ext-3", Reply("✅ 已保存：t"))
    assert result.ok and ch.intake.replies == [("ext-3", "✅ 已保存：t")]


async def test_a_port_in_use_is_said_in_chinese():
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    try:
        ch = ExtensionChannel(TOKEN, port=taken.getsockname()[1])
        with pytest.raises(RuntimeError, match="Chrome 插件入口没能启动"):
            await ch.start(None)
    finally:
        taken.close()


@pytest.mark.parametrize("length", ["9" * 5000, "-1", "12a", "1 2", "99999999999"], ids=["huge", "neg", "alpha", "space", "11"])
async def test_a_bad_length_is_400_without_a_traceback(server, length, caplog):
    head = (f"POST /save HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nAuthorization: Bearer {TOKEN}\r\n"
            f"Content-Length: {length}\r\n\r\n")
    status, _, _ = await call(server, "POST", "/save", raw_head=head)
    assert status == 400
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_list_that_breaks_is_500_and_the_server_goes_on(server):
    async def boom():
        raise RuntimeError("bug")

    real = server.intake.recent
    server.intake.recent = boom
    status, _, body = await call(server, "GET", "/recent", auth())
    assert status == 500 and json.loads(body)["error"]
    server.intake.recent = real
    assert (await call(server, "GET", "/recent", auth()))[0] == 200


async def test_shutdown_does_not_wait_for_a_hanging_connection():
    ch = ExtensionChannel(TOKEN, port=0)
    ch.intake = FakeIntake()
    task = asyncio.create_task(ch.start(None))
    await asyncio.wait_for(ch.ready.wait(), 5)
    reader, writer = await asyncio.open_connection("127.0.0.1", ch.port)
    writer.write(b"GET /recent HTTP/1.1\r\n")  # never finished: the head timeout is 10 s
    await writer.drain()
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
    writer.close()


# ---- P23: macOS Shortcuts (Safari share sheet, the Services menu) post to the same endpoint ----

async def test_a_shortcut_with_no_origin_saves_with_the_token(server):
    """Shortcuts sends no Origin: the token alone lets it in, and the answer carries no CORS header."""
    body = json.dumps({"kind": "page", "url": "https://a.com/article"}).encode()
    status, headers, payload = await call(server, "POST", "/save",
                                          auth(**{"Content-Type": "application/json", "User-Agent": "Shortcuts"}), body)
    assert status == 202 and json.loads(payload) == {"id": 1}
    assert "access-control-allow-origin" not in headers
    req = server.intake.saved[0]
    assert req.kind == "page" and req.url == "https://a.com/article" and req.html is None


async def test_a_shortcut_selection_without_a_link_is_saved(server):
    body = json.dumps({"kind": "selection", "text": "  在别的 App 里选中的字  "}).encode()
    status, _, _ = await call(server, "POST", "/save", auth(), body)
    assert status == 202
    req = server.intake.saved[0]
    assert req.kind == "selection" and req.text == "在别的 App 里选中的字" and req.url is None


async def test_a_shortcut_without_the_token_is_refused(server):
    body = json.dumps({"kind": "page", "url": "https://a.com/article"}).encode()
    status, _, _ = await call(server, "POST", "/save", {"Authorization": "Bearer wrong"}, body)
    assert status == 401 and server.intake.saved == []


# ---- P22: inside a container the endpoint listens on every address; the Host check stays loopback ----

@pytest.fixture
async def container_server():
    ch = ExtensionChannel(TOKEN, port=0, bind="0.0.0.0")
    ch.intake = FakeIntake()
    task = asyncio.create_task(ch.start(None))
    await asyncio.wait_for(ch.ready.wait(), 5)
    yield ch
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_bind_all_addresses_for_docker(container_server):
    assert container_server.server.sockets[0].getsockname()[0] == "0.0.0.0"
    status, _, _ = await call(container_server, "GET", "/recent", auth())  # Host 127.0.0.1:<port>: published as is
    assert status == 200
    status, _, _ = await call(container_server, "GET", "/recent", auth(), host=f"localhost:{container_server.port}")
    assert status == 200


@pytest.mark.parametrize("host", ["0.0.0.0:{port}", "evil.example.com:{port}", "172.17.0.2:{port}"])
async def test_bind_all_still_refuses_other_host_headers(container_server, host):
    status, _, _ = await call(container_server, "GET", "/recent", auth(), host=host.format(port=container_server.port))
    assert status == 403


async def test_the_bind_address_comes_from_the_config(tmp_path):
    from octopus.bootstrap import build_channels
    from octopus.config import make_test_config

    class Secrets:
        def get(self, name):
            return TOKEN if name == "extension_token" else None

    cfg = make_test_config(tmp_path)
    cfg.extension.enabled = True
    channels, _ = build_channels(cfg, Secrets(), notify=None)
    assert channels["extension"].bind == "127.0.0.1"
    cfg.extension.bind = "0.0.0.0"
    channels, _ = build_channels(cfg, Secrets(), notify=None)
    assert channels["extension"].bind == "0.0.0.0"


async def _start_and_stop(ch):
    task = asyncio.create_task(ch.start(None))
    await asyncio.wait_for(ch.ready.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_listening_beyond_this_machine_outside_a_container_is_warned_once(monkeypatch, caplog):
    from octopus import host

    monkeypatch.setattr(host, "in_container", lambda: False)
    ch = ExtensionChannel(TOKEN, port=0, bind="0.0.0.0")
    await _start_and_stop(ch)
    ch.port = 0
    await _start_and_stop(ch)  # a supervised restart: not said again
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "0.0.0.0" in r.getMessage()]
    assert len(warnings) == 1 and "所有网卡" in warnings[0].getMessage()


@pytest.mark.parametrize("bind,container", [("0.0.0.0", True), ("127.0.0.1", False)])
async def test_no_warning_in_a_container_or_on_loopback(monkeypatch, caplog, bind, container):
    from octopus import host

    monkeypatch.setattr(host, "in_container", lambda: container)
    await _start_and_stop(ExtensionChannel(TOKEN, port=0, bind=bind))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---- integration (P22 + P23): a Shortcuts-shaped request (no Origin) against a container's 0.0.0.0 bind ----

async def test_a_shortcut_through_a_published_container_port_is_held_to_the_same_rules(container_server):
    body = json.dumps({"kind": "page", "url": "https://a.com/article"}).encode()
    status, headers, _ = await call(container_server, "POST", "/save", auth(), body)  # Host 127.0.0.1:<port>
    assert status == 202 and "access-control-allow-origin" not in headers
    status, _, _ = await call(container_server, "POST", "/save", {"Authorization": "Bearer wrong"}, body)
    assert status == 401
    for host in (f"172.17.0.2:{container_server.port}", f"192.168.1.20:{container_server.port}"):  # the LAN / bridge
        status, _, _ = await call(container_server, "POST", "/save", auth(), body, host=host)
        assert status == 403
    assert len(container_server.intake.saved) == 1
