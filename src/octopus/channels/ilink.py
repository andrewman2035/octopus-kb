"""WeChat ClawBot iLink protocol client, modelled on Tencent's MIT client @tencent-weixin/openclaw-weixin 2.4.10."""
# ---- Third-party attribution -------------------------------------------------------------------------------------
# This module follows the protocol as implemented by the upstream project @tencent-weixin/openclaw-weixin
# (version 2.4.10, npm), which is released under the MIT License. If any part of this file is a translation of that
# project's code, the upstream copyright notice and the MIT permission notice below apply to that part. Source of
# the notice: the LICENSE file of the published package,
# https://unpkg.com/@tencent-weixin/openclaw-weixin@2.4.10/LICENSE
# ("Tencent is pleased to support the open source community by making openclaw-weixin available. ...
# openclaw-weixin is licensed under the MIT.").
#
#   Copyright (C) 2026 Tencent.  All rights reserved.
#
#   Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
#   documentation files (the "Software"), to deal in the Software without restriction, including without limitation
#   the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and
#   to permit persons to whom the Software is furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in all copies or substantial portions
#   of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
#   THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
#   CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
#   IN THE SOFTWARE.
# -------------------------------------------------------------------------------------------------------------------
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import quote, urlparse

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from octopus.i18n import t

ILINK_BASE = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "2.4.10"
CLIENT_VERSION = str((2 << 16) | (4 << 8) | 10)
STALE_TOKEN = -14
APP_HEADERS = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": CLIENT_VERSION}
BASE_INFO = {"channel_version": CHANNEL_VERSION, "bot_agent": "Octopus/0.1"}
MAX_MEDIA_BYTES = 100 * 1024 * 1024
POLL_PAUSE = 1.0
MAX_VERIFY_ATTEMPTS = 3
_HOSTNAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")


class ILinkError(Exception):
    def __init__(self, code: int | None, message: str):
        super().__init__(message)
        self.code = code


class ILinkTimeout(ILinkError):
    """Client-side read timeout (an empty long poll is normal)."""


@dataclass
class LoginResult:
    bot_token: str = field(repr=False)
    base_url: str
    bot_id: str | None
    user_id: str | None


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def uin_header() -> str:
    return base64.b64encode(str(int.from_bytes(os.urandom(4), "big")).encode()).decode()


def parse_aes_key(b64: str) -> bytes:
    raw = base64.b64decode(b64)
    if len(raw) == 16:
        return raw
    if len(raw) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", raw):
        return bytes.fromhex(raw.decode())
    raise ValueError(f"bad aes_key length {len(raw)}")


def ecb_decrypt(data: bytes, key: bytes) -> bytes:
    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    plain = d.update(data) + d.finalize()
    u = padding.PKCS7(128).unpadder()
    return u.update(plain) + u.finalize()


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError as e:
        raise ILinkError(None, t("ilink.bad_json")) from e
    if not isinstance(data, dict):
        raise ILinkError(None, t("ilink.bad_response"))
    return data


def _error_code(data: dict) -> int:
    for key in ("ret", "errcode"):
        raw = data.get(key)
        if raw in (None, ""):
            continue
        try:
            code = int(raw)
        except (TypeError, ValueError) as e:
            raise ILinkError(None, t("ilink.bad_code", key=key)) from e
        if code:
            return code
    return 0


def _check_base_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ILinkError(None, t("ilink.bad_base_url"))
    return url


class ILinkClient:
    def __init__(self, token: str | None = None, base_url: str = ILINK_BASE, http: httpx.AsyncClient | None = None):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0))

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
             "X-WECHAT-UIN": uin_header(), **APP_HEADERS}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _http(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            r = await self.http.request(method, url, **kwargs)
            r.raise_for_status()
            return r
        except httpx.ReadTimeout as e:
            raise ILinkTimeout(None, t("ilink.read_timeout")) from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            raise ILinkError(STALE_TOKEN if status == 401 else status, f"HTTP {status}") from e
        except httpx.HTTPError as e:
            raise ILinkError(None, t("ilink.network", error=type(e).__name__)) from e

    async def _post(self, path: str, body: dict, timeout=httpx.USE_CLIENT_DEFAULT) -> dict:
        r = await self._http("POST", f"{self.base_url}/{path}", headers=self._headers(),
                             content=json.dumps({**body, "base_info": BASE_INFO}, ensure_ascii=False),
                             timeout=timeout)
        data = _json(r)
        code = _error_code(data)
        if code:
            raise ILinkError(code, str(data.get("errmsg") or t("ilink.error_code", code=code)))
        return data

    async def get_qrcode(self) -> tuple[str, str]:
        r = await self._http("POST", f"{ILINK_BASE}/ilink/bot/get_bot_qrcode?bot_type=3", headers=self._headers(),
                             content=json.dumps({"local_token_list": []}))
        data = _json(r)
        try:
            return str(data["qrcode"]), str(data["qrcode_img_content"])
        except KeyError as e:
            raise ILinkError(None, t("ilink.qr_incomplete")) from e

    async def poll_login(self, qrcode: str, verify_code: str | None = None, host: str = ILINK_BASE) -> dict:
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        try:
            r = await self._http("GET", f"{host}/ilink/bot/get_qrcode_status", params=params, headers=APP_HEADERS,
                                 timeout=45.0)
        except ILinkTimeout:
            return {"status": "wait"}
        return _json(r)

    async def login(self, qrcode: str, on_status: Callable[[str], None] | None = None,
                    verify_code_fn: Callable[[], "str | None | Awaitable[str | None]"] | None = None,
                    timeout: float = 480) -> LoginResult:
        loop = asyncio.get_running_loop()
        host, code, prompts = ILINK_BASE, None, 0
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            started = loop.time()
            s = await self.poll_login(qrcode, code, host)
            code = None
            status = s.get("status", "wait")
            if on_status:
                on_status(status)
            if status == "confirmed":
                token = s.get("bot_token")
                if not isinstance(token, str) or not token:
                    raise ILinkError(None, t("ilink.no_bot_token"))
                base = _check_base_url(s["baseurl"]) if s.get("baseurl") else ILINK_BASE
                return LoginResult(token, base, s.get("ilink_bot_id"), s.get("ilink_user_id"))
            if status in ("expired", "verify_code_blocked"):
                raise ILinkError(None, t("ilink.login_failed", status=status))
            if status == "scaned_but_redirect" and s.get("redirect_host"):
                redirect = str(s["redirect_host"])
                if not _HOSTNAME.fullmatch(redirect):
                    raise ILinkError(None, t("ilink.bad_redirect"))
                host = "https://" + redirect
            if status == "need_verifycode":
                if verify_code_fn is None:
                    raise ILinkError(None, t("ilink.login_failed", status=status))
                prompts += 1
                if prompts > MAX_VERIFY_ATTEMPTS:
                    raise ILinkError(None, t("ilink.too_many_codes"))
                answer = verify_code_fn()
                if inspect.isawaitable(answer):
                    answer = await answer
                answer = (answer or "").strip()
                if answer:
                    code = answer
                    continue
            elapsed = loop.time() - started
            if elapsed < POLL_PAUSE:
                await _pause(POLL_PAUSE - elapsed)
        raise ILinkError(None, t("ilink.login_timeout"))

    async def get_updates(self, buf: str) -> tuple[list[dict], str, float]:
        try:
            data = await self._post("ilink/bot/getupdates", {"get_updates_buf": buf}, timeout=45.0)
        except ILinkTimeout:
            return [], buf, 35.0
        msgs = data.get("msgs") or []
        if not isinstance(msgs, list):
            raise ILinkError(None, t("ilink.bad_updates"))
        try:
            timeout_ms = float(data.get("longpolling_timeout_ms") or 35000)
        except (TypeError, ValueError):
            timeout_ms = 35000.0
        return msgs, str(data.get("get_updates_buf") or buf), timeout_ms / 1000

    async def send_text(self, to_user_id: str, text: str, context_token: str) -> str | None:
        body = {"msg": {"from_user_id": "", "to_user_id": to_user_id, "client_id": f"octopus:{uuid.uuid4().hex}",
                        "message_type": 2, "message_state": 2, "context_token": context_token,
                        "item_list": [{"type": 1, "text_item": {"text": text}}]}}
        data = await self._post("ilink/bot/sendmessage", body)
        mid = data.get("message_id")
        return str(mid) if mid else None

    async def download_media(self, media: dict, aeskey_hex: str | None = None) -> bytes:
        too_big = ILinkError(None, t("ilink.too_big"))
        try:
            url = media.get("full_url") or (
                f"{CDN_BASE}/download?encrypted_query_param={quote(media['encrypt_query_param'], safe='')}")
            key = None
            if aeskey_hex:
                key = bytes.fromhex(aeskey_hex)
            elif media.get("aes_key"):
                key = parse_aes_key(media["aes_key"])
        except (KeyError, TypeError, AttributeError, ValueError) as e:
            raise ILinkError(None, t("ilink.bad_media")) from e
        buf = bytearray()
        try:
            async with self.http.stream("GET", url, timeout=120.0) as r:
                r.raise_for_status()
                length = r.headers.get("content-length")
                if length and length.isdigit() and int(length) > MAX_MEDIA_BYTES:
                    raise too_big
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_MEDIA_BYTES:
                        raise too_big
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            raise ILinkError(STALE_TOKEN if status == 401 else status, f"HTTP {status}") from e
        except httpx.HTTPError as e:
            raise ILinkError(None, t("ilink.network", error=type(e).__name__)) from e
        if key is None:
            return bytes(buf)
        try:
            return ecb_decrypt(bytes(buf), key)
        except (ValueError, TypeError) as e:
            raise ILinkError(None, t("ilink.decrypt_failed")) from e

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()
