import base64
import json
import os

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from octopus.channels.ilink import (CDN_BASE, ILINK_BASE, STALE_TOKEN, ILinkClient, ILinkError, ecb_decrypt,
                                    parse_aes_key, uin_header)


def encrypt(data, key):
    p = padding.PKCS7(128).padder()
    e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return e.update(p.update(data) + p.finalize()) + e.finalize()


def test_aes_key_formats():
    raw = os.urandom(16)
    assert parse_aes_key(base64.b64encode(raw).decode()) == raw
    assert parse_aes_key(base64.b64encode(raw.hex().encode()).decode()) == raw
    with pytest.raises(ValueError):
        parse_aes_key(base64.b64encode(b"short").decode())


def test_ecb_round_trip():
    key = os.urandom(16)
    assert ecb_decrypt(encrypt(b"hello world", key), key) == b"hello world"


def test_uin_header_is_base64_digits():
    assert base64.b64decode(uin_header()).decode().isdigit()


@respx.mock
async def test_login_flow_with_redirect(pauses):
    respx.post(url__startswith=f"{ILINK_BASE}/ilink/bot/get_bot_qrcode").mock(return_value=httpx.Response(
        200, json={"qrcode": "q1", "qrcode_img_content": "https://weixin.qq.com/x"}))
    status = respx.get(url__startswith=f"{ILINK_BASE}/ilink/bot/get_qrcode_status").mock(side_effect=[
        httpx.Response(200, json={"status": "wait"}),
        httpx.Response(200, json={"status": "scaned_but_redirect", "redirect_host": "alt.example.com"})])
    respx.get(url__startswith="https://alt.example.com/ilink/bot/get_qrcode_status").mock(return_value=httpx.Response(
        200, json={"status": "confirmed", "bot_token": "tok", "baseurl": "https://alt.example.com",
                   "ilink_bot_id": "b@im.bot", "ilink_user_id": "u@im.wechat"}))
    c = ILinkClient()
    qr, content = await c.get_qrcode()
    assert (qr, content) == ("q1", "https://weixin.qq.com/x")
    seen = []
    res = await c.login(qr, on_status=seen.append)
    assert (res.bot_token, res.base_url, res.bot_id, res.user_id) == ("tok", "https://alt.example.com", "b@im.bot", "u@im.wechat")
    assert seen[:2] == ["wait", "scaned_but_redirect"]
    assert len(pauses) == 2  # the redirect poll is paced like "wait"
    assert status.calls[0].request.headers["iLink-App-Id"] == "bot"


@respx.mock
async def test_login_expired():
    respx.get(url__startswith=f"{ILINK_BASE}/ilink/bot/get_qrcode_status").mock(return_value=httpx.Response(
        200, json={"status": "expired"}))
    with pytest.raises(ILinkError):
        await ILinkClient().login("q1")


@respx.mock
async def test_get_updates_and_stale_token():
    route = respx.post(f"{ILINK_BASE}/ilink/bot/getupdates").mock(side_effect=[
        httpx.Response(200, json={"ret": 0, "msgs": [{"message_id": 1}], "get_updates_buf": "b2", "longpolling_timeout_ms": 30000}),
        httpx.Response(200, json={"ret": STALE_TOKEN, "errmsg": "expired"})])
    c = ILinkClient(token="tok")
    msgs, buf, timeout = await c.get_updates("b1")
    assert (msgs, buf, timeout) == ([{"message_id": 1}], "b2", 30.0)
    body = json.loads(route.calls[0].request.content)
    assert body["get_updates_buf"] == "b1" and body["base_info"]["channel_version"] == "2.4.10"
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"
    with pytest.raises(ILinkError) as ei:
        await c.get_updates("b2")
    assert ei.value.code == STALE_TOKEN


@respx.mock
async def test_get_updates_client_timeout_is_empty():
    respx.post(f"{ILINK_BASE}/ilink/bot/getupdates").mock(side_effect=httpx.ReadTimeout("slow"))
    msgs, buf, _ = await ILinkClient(token="tok").get_updates("b1")
    assert msgs == [] and buf == "b1"


@respx.mock
async def test_send_text_and_fake_success():
    route = respx.post(f"{ILINK_BASE}/ilink/bot/sendmessage").mock(side_effect=[
        httpx.Response(200, json={"ret": 0, "message_id": "123456789012345678"}),
        httpx.Response(200, json={"ret": 0}),
        httpx.Response(200, json={"ret": -2, "errmsg": "rate limited"})])
    c = ILinkClient(token="tok")
    assert await c.send_text("u@im.wechat", "你好", "ctx") == "123456789012345678"
    msg = json.loads(route.calls[0].request.content)["msg"]
    assert msg["to_user_id"] == "u@im.wechat" and msg["context_token"] == "ctx"
    assert msg["message_type"] == 2 and msg["item_list"] == [{"type": 1, "text_item": {"text": "你好"}}]
    assert await c.send_text("u@im.wechat", "x", "ctx") is None
    with pytest.raises(ILinkError):
        await c.send_text("u@im.wechat", "x", "ctx")


@respx.mock
async def test_download_media_decrypts():
    key = os.urandom(16)
    respx.get(url__startswith=f"{CDN_BASE}/download").mock(return_value=httpx.Response(200, content=encrypt(b"IMG", key)))
    media = {"encrypt_query_param": "a b", "aes_key": base64.b64encode(key).decode()}
    assert await ILinkClient(token="tok").download_media(media) == b"IMG"
    respx.get("https://cdn.example.com/full").mock(return_value=httpx.Response(200, content=encrypt(b"IMG2", key)))
    assert await ILinkClient(token="tok").download_media({"full_url": "https://cdn.example.com/full"}, aeskey_hex=key.hex()) == b"IMG2"


# ---- fix round 1 ----

from octopus.channels import ilink  # noqa: E402


@pytest.fixture
def pauses(monkeypatch):
    calls = []

    async def fake(seconds):
        calls.append(seconds)

    monkeypatch.setattr(ilink, "_pause", fake)
    return calls


async def test_post_uses_client_timeout_not_none():
    seen = []

    def handler(request):
        seen.append(dict(request.extensions["timeout"]))
        return httpx.Response(200, json={"ret": 0, "message_id": "1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(45.0, connect=15.0))
    await ILinkClient(token="t", http=http).send_text("u", "x", "c")
    assert seen[0]["read"] == 45.0 and seen[0]["connect"] == 15.0
    assert None not in seen[0].values()


@respx.mock
async def test_http_status_errors_are_wrapped():
    respx.post(f"{ILINK_BASE}/ilink/bot/sendmessage").mock(side_effect=[
        httpx.Response(500), httpx.Response(401), httpx.Response(403)])
    c = ILinkClient(token="t")
    codes = []
    for _ in range(3):
        with pytest.raises(ILinkError) as e:
            await c.send_text("u", "x", "c")
        codes.append(e.value.code)
    assert codes == [500, STALE_TOKEN, 403]  # only 401 means the login expired (final review M6)


@respx.mock
@pytest.mark.parametrize("resp", [httpx.Response(200, content=b"<html>"), httpx.Response(200, json=[1, 2]),
                                  httpx.Response(200, json={"ret": "abc"})])
async def test_bad_bodies_are_wrapped(resp):
    respx.post(f"{ILINK_BASE}/ilink/bot/sendmessage").mock(return_value=resp)
    with pytest.raises(ILinkError):
        await ILinkClient(token="t").send_text("u", "x", "c")


@respx.mock
async def test_missing_keys_are_wrapped():
    respx.post(url__startswith=f"{ILINK_BASE}/ilink/bot/get_bot_qrcode").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(ILinkError):
        await ILinkClient().get_qrcode()
    respx.get(url__startswith=f"{ILINK_BASE}/ilink/bot/get_qrcode_status").mock(
        return_value=httpx.Response(200, json={"status": "confirmed"}))
    with pytest.raises(ILinkError):
        await ILinkClient().login("q")


@respx.mock
async def test_string_zero_ret_is_success():
    respx.post(f"{ILINK_BASE}/ilink/bot/sendmessage").mock(return_value=httpx.Response(200, json={"ret": "0", "errcode": 0, "message_id": "9"}))
    assert await ILinkClient(token="t").send_text("u", "x", "c") == "9"


@respx.mock
@pytest.mark.parametrize("exc", [httpx.ConnectError("down"), httpx.ConnectTimeout("slow"),
                                 httpx.RemoteProtocolError("bad"), httpx.PoolTimeout("busy")])
async def test_transport_errors_are_visible(exc):
    respx.post(f"{ILINK_BASE}/ilink/bot/getupdates").mock(side_effect=exc)
    with pytest.raises(ILinkError):
        await ILinkClient(token="t").get_updates("b")


@respx.mock
async def test_transport_error_in_other_methods():
    respx.post(url__startswith=f"{ILINK_BASE}/ilink/bot/get_bot_qrcode").mock(side_effect=httpx.ConnectError("x"))
    respx.get(url__startswith=f"{ILINK_BASE}/ilink/bot/get_qrcode_status").mock(side_effect=httpx.ConnectError("x"))
    respx.get("https://cdn.example.com/f").mock(side_effect=httpx.ConnectError("x"))
    c = ILinkClient(token="t")
    with pytest.raises(ILinkError):
        await c.get_qrcode()
    with pytest.raises(ILinkError):
        await c.poll_login("q")
    with pytest.raises(ILinkError):
        await c.download_media({"full_url": "https://cdn.example.com/f"})


@respx.mock
async def test_download_rejects_oversized_content_length(monkeypatch):
    monkeypatch.setattr(ilink, "MAX_MEDIA_BYTES", 10)
    respx.get("https://cdn.example.com/big").mock(return_value=httpx.Response(200, content=b"x" * 20))
    with pytest.raises(ILinkError, match="100MB"):
        await ILinkClient(token="t").download_media({"full_url": "https://cdn.example.com/big"})


async def test_download_rejects_oversized_stream_without_length(monkeypatch):
    monkeypatch.setattr(ilink, "MAX_MEDIA_BYTES", 10)

    async def body():
        for _ in range(5):
            yield b"xxxx"

    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body())))
    with pytest.raises(ILinkError, match="100MB"):
        await ILinkClient(token="t", http=http).download_media({"full_url": "https://cdn.example.com/big"})


def test_media_cap_is_100mb():
    assert ilink.MAX_MEDIA_BYTES == 100 * 1024 * 1024


@respx.mock
async def test_download_failures_are_wrapped():
    key = os.urandom(16)
    respx.get("https://cdn.example.com/f").mock(return_value=httpx.Response(200, content=b"not a multiple of 16"))
    c = ILinkClient(token="t")
    with pytest.raises(ILinkError):  # bad ciphertext
        await c.download_media({"full_url": "https://cdn.example.com/f"}, aeskey_hex=key.hex())
    with pytest.raises(ILinkError):  # bad hex key
        await c.download_media({"full_url": "https://cdn.example.com/f"}, aeskey_hex="zz")
    with pytest.raises(ILinkError):  # bad base64 key
        await c.download_media({"full_url": "https://cdn.example.com/f", "aes_key": "!!"})
    with pytest.raises(ILinkError):  # missing query param and url
        await c.download_media({})
    respx.get("https://cdn.example.com/404").mock(return_value=httpx.Response(404))
    with pytest.raises(ILinkError) as e:
        await c.download_media({"full_url": "https://cdn.example.com/404"})
    assert e.value.code == 404


def test_login_result_repr_hides_token():
    r = ilink.LoginResult("SECRET-TOKEN", "https://x", "b", "u")
    assert "SECRET-TOKEN" not in repr(r) and r.bot_token == "SECRET-TOKEN"


@respx.mock
async def test_login_paces_wait_and_unknown_statuses(pauses):
    respx.get(url__startswith=f"{ILINK_BASE}/ilink/bot/get_qrcode_status").mock(side_effect=[
        httpx.Response(200, json={"status": "wait"}),
        httpx.Response(200, json={"status": "mystery"}),
        httpx.Response(200, json={"status": "confirmed", "bot_token": "tok"})])
    res = await ILinkClient().login("q")
    assert res.bot_token == "tok" and res.base_url == ILINK_BASE
    assert len(pauses) == 2 and all(0 < p <= 1.0 for p in pauses)


@respx.mock
@pytest.mark.parametrize("make_fn", [lambda: (lambda: "123456"), lambda: _async_code])
async def test_login_verify_code_success(pauses, make_fn):
    route = respx.get(url__startswith=f"{ILINK_BASE}/ilink/bot/get_qrcode_status").mock(side_effect=[
        httpx.Response(200, json={"status": "need_verifycode"}),
        httpx.Response(200, json={"status": "confirmed", "bot_token": "tok"})])
    res = await ILinkClient().login("q", verify_code_fn=make_fn())
    assert res.bot_token == "tok"
    assert route.calls[1].request.url.params["verify_code"] == "123456"
    assert "verify_code" not in route.calls[0].request.url.params


async def _async_code():
    return "123456"


@respx.mock
async def test_login_empty_verify_code_asks_again_then_fails(pauses):
    route = respx.get(url__startswith=f"{ILINK_BASE}/ilink/bot/get_qrcode_status").mock(
        return_value=httpx.Response(200, json={"status": "need_verifycode"}))
    asked = []

    def fn():
        asked.append(1)
        return "  "

    with pytest.raises(ILinkError):
        await ILinkClient().login("q", verify_code_fn=fn)
    assert len(asked) == 3 and route.call_count == 4 and len(pauses) == 3


@respx.mock
async def test_login_verify_code_without_fn_fails():
    respx.get(url__startswith=f"{ILINK_BASE}/ilink/bot/get_qrcode_status").mock(
        return_value=httpx.Response(200, json={"status": "need_verifycode"}))
    with pytest.raises(ILinkError):
        await ILinkClient().login("q")


@respx.mock
@pytest.mark.parametrize("host", ["evil.com/path", "a b", "evil.com:80@x", "", "https://x.com"])
async def test_login_rejects_bad_redirect_host(host):
    respx.get(url__startswith=f"{ILINK_BASE}/ilink/bot/get_qrcode_status").mock(
        return_value=httpx.Response(200, json={"status": "scaned_but_redirect", "redirect_host": host or "/"}))
    with pytest.raises(ILinkError):
        await ILinkClient().login("q")


@respx.mock
async def test_login_rejects_non_https_baseurl():
    respx.get(url__startswith=f"{ILINK_BASE}/ilink/bot/get_qrcode_status").mock(return_value=httpx.Response(
        200, json={"status": "confirmed", "bot_token": "tok", "baseurl": "http://evil.example.com"}))
    with pytest.raises(ILinkError):
        await ILinkClient().login("q")


async def test_close_only_closes_owned_client():
    shared = httpx.AsyncClient()
    await ILinkClient(token="t", http=shared).close()
    assert not shared.is_closed
    await shared.aclose()
    own = ILinkClient(token="t")
    await own.close()
    assert own.http.is_closed
