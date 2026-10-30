"""Feishu (Lark) self-built app bot over the long connection (lark-oapi WebSocket client on its own thread)."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from octopus.channels.base import (Button, OnMessage, Reply, SendResult, log_dropped, safe_filename, split_text,
                                   tell_dropped)
from octopus.i18n import aliases, t
from octopus.models import Attachment, IncomingMessage

log = logging.getLogger(__name__)
MAX_TEXT = 10000
MAX_DOWNLOAD = 50_000_000
MAX_UPLOAD = 30_000_000  # Feishu's limit for a file message
__getattr__ = aliases({"TOO_BIG": "feishu.too_big", "DOWNLOAD_FAILED": "channels.download_failed"})
CARD_DEDUPE_SECONDS = 10
WATCHDOG_INTERVAL = 60
WATCHDOG_LIMIT = 3


class _TooBig(Exception):
    pass


def watchdog_step(connected: bool, misses: int, limit: int = WATCHDOG_LIMIT) -> tuple[int, bool]:
    """One watchdog check: returns (new consecutive-miss count, whether the connection should be declared dead)."""
    if connected:
        return 0, False
    misses += 1
    return misses, misses >= limit


def shutdown_ws(client, ws_loop: asyncio.AbstractEventLoop, timeout: float = 5) -> None:
    """Best-effort stop of the SDK client living on `ws_loop` (blocking; call from a worker thread)."""
    fut = None
    client._auto_reconnect = False  # otherwise the SDK reconnects behind our back while we shut down
    try:
        fut = asyncio.run_coroutine_threadsafe(client._disconnect(), ws_loop)
        fut.result(timeout)
    except BaseException:
        log.warning("feishu: could not disconnect cleanly", exc_info=True)
        if fut is not None:
            fut.cancel()
    try:
        ws_loop.call_soon_threadsafe(ws_loop.stop)
    except RuntimeError:  # loop already closed
        pass


def card_json(markdown: str, buttons: list[Button], plain: bool = False) -> dict:
    """plain: the text as plain text (as a text message shows it), not Markdown."""
    elements = [{"tag": "div", "text": {"tag": "plain_text", "content": markdown}} if plain
                else {"tag": "markdown", "content": markdown}]
    for b in buttons:
        elements.append({"tag": "button", "text": {"tag": "plain_text", "content": b.label}, "type": "default",
                         "behaviors": [{"type": "callback", "value": {"fix": b.data}}]})
    return {"schema": "2.0", "body": {"elements": elements}}


class LarkApi:
    """Thin async wrapper over lark-oapi; the only place that talks to Feishu's HTTP API."""

    def __init__(self, app_id: str, app_secret: str):
        import lark_oapi as lark

        self.lark = lark
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    async def _create(self, open_id: str, msg_type: str, content: dict) -> str | None:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        req = (CreateMessageRequest.builder().receive_id_type("open_id")
               .request_body(CreateMessageRequestBody.builder().receive_id(open_id).msg_type(msg_type)
                             .content(json.dumps(content, ensure_ascii=False)).uuid(uuid.uuid4().hex).build())
               .build())
        resp = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            raise RuntimeError(t("feishu.send_failed", code=resp.code, msg=resp.msg))
        return resp.data.message_id

    async def send_text(self, open_id: str, text: str) -> str | None:
        return await self._create(open_id, "text", {"text": text})

    async def send_card(self, open_id: str, markdown: str, buttons: list[Button], plain: bool = False) -> str | None:
        return await self._create(open_id, "interactive", card_json(markdown, buttons, plain))

    async def send_image(self, open_id: str, path: Path) -> str | None:
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        def upload():
            with open(path, "rb") as fh:
                req = CreateImageRequest.builder().request_body(
                    CreateImageRequestBody.builder().image_type("message").image(fh).build()).build()
                return self.client.im.v1.image.create(req)

        resp = await asyncio.to_thread(upload)
        if not resp.success():
            raise RuntimeError(t("feishu.upload_failed", code=resp.code, msg=resp.msg))
        return await self._create(open_id, "image", {"image_key": resp.data.image_key})

    async def send_file(self, open_id: str, data: bytes, filename: str) -> str | None:
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        def upload():
            req = CreateFileRequest.builder().request_body(
                CreateFileRequestBody.builder().file_type("stream").file_name(filename).file(io.BytesIO(data))
                .build()).build()
            return self.client.im.v1.file.create(req)

        resp = await asyncio.to_thread(upload)
        if not resp.success():
            raise RuntimeError(t("feishu.upload_failed", code=resp.code, msg=resp.msg))
        return await self._create(open_id, "file", {"file_key": resp.data.file_key})

    async def download(self, message_id: str, key: str, kind: str) -> bytes:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        req = GetMessageResourceRequest.builder().message_id(message_id).file_key(key).type(kind).build()
        resp = await self.client.im.v1.message_resource.aget(req)
        if not resp.success():
            raise RuntimeError(t("feishu.download_failed", code=resp.code, msg=resp.msg))
        return resp.file.read()


class FeishuChannel:
    name = "feishu"
    acks_saves = True

    def __init__(self, app_id: str, app_secret: str, incoming_dir: Path, api=None,
                 allowed: Callable[[str], bool] | None = None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.incoming_dir = incoming_dir
        self.api = api
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        self.allowed = allowed  # checked before anything is downloaded or sent
        self._cards: dict[tuple[str, str], float] = {}
        self._ws_thread: threading.Thread | None = None

    def _is_allowed(self, user_id: str) -> bool:
        if self.allowed is None:
            return True
        try:
            ok = bool(self.allowed(user_id))
        except Exception:
            log.exception("feishu allow-list check failed")
            ok = False
        if not ok:
            log.info("feishu: ignoring message from user %s (add it to allowed_users to accept)", user_id)
        return ok

    async def _save(self, message_id: str, key: str, kind: str, filename: str, staged: list[Path]) -> Path:
        data = await self.api.download(message_id, key, "image" if kind == "image" else "file")
        if len(data) > MAX_DOWNLOAD:
            raise _TooBig
        dest = self.incoming_dir / uuid.uuid4().hex
        path = dest / safe_filename(filename, "file")
        staged.append(dest)  # recorded before writing so a failed write is cleaned up too

        def write() -> None:
            dest.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(write)
        return path

    def _first_time(self, message_id: str) -> bool:
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        while len(self._seen) > 1000:
            self._seen.popitem(last=False)
        return True

    async def to_incoming(self, data) -> IncomingMessage | None:
        msg, sender = data.event.message, data.event.sender
        if msg.chat_type != "p2p" or getattr(sender, "sender_type", "user") != "user":
            return None
        user_id = sender.sender_id.open_id
        if not self._is_allowed(user_id):
            return None
        if not self._first_time(msg.message_id):
            return None
        staged: list[Path] = []
        try:
            return await self._parse(msg, user_id, staged)
        except Exception as e:
            for d in staged:
                shutil.rmtree(d, ignore_errors=True)
            if isinstance(e, _TooBig):
                await tell_dropped(self, user_id, "too big", t("feishu.too_big"))
            else:
                log.exception("feishu: could not read incoming message")
                await tell_dropped(self, user_id, "download failed", t("channels.download_failed"))
            return None
        except BaseException:  # a stop mid-download: nothing staged stays behind either
            for d in staged:
                shutil.rmtree(d, ignore_errors=True)
            raise

    async def _parse(self, msg, user_id: str, staged: list[Path]) -> IncomingMessage | None:
        content = json.loads(msg.content or "{}")
        text, attachments = "", []
        mtype = msg.message_type
        if mtype == "text":
            text = content.get("text", "")
        elif mtype == "image":
            p = await self._save(msg.message_id, content["image_key"], "image", f"{content['image_key']}.png", staged)
            attachments.append(Attachment(kind="image", path=p, filename=p.name))
        elif mtype in ("file", "audio", "media"):
            kind = {"file": "file", "audio": "audio", "media": "video"}[mtype]
            default = {"file": "file", "audio": "audio.opus", "media": "video.mp4"}[mtype]
            p = await self._save(msg.message_id, content["file_key"], kind, content.get("file_name") or default, staged)
            # Feishu "audio" messages are hold-to-talk recordings (audio files arrive as "file")
            attachments.append(Attachment(kind=kind, path=p, filename=p.name, voice=mtype == "audio"))
        elif mtype == "post":
            if "content" not in content:  # some payloads wrap the post by locale: {"zh_cn": {...}}
                content = next((v for v in content.values() if isinstance(v, dict) and "content" in v), {})
            lines = [content.get("title", "")] if content.get("title") else []
            for para in content.get("content", []):
                parts = []
                for node in para:
                    tag = node.get("tag")
                    if tag in ("text", "md"):
                        parts.append(node.get("text", ""))
                    elif tag == "a":
                        parts.append(f"{node.get('text', '')} {node.get('href', '')}".strip())
                    elif tag == "img":
                        p = await self._save(msg.message_id, node["image_key"], "image", f"{node['image_key']}.png", staged)
                        attachments.append(Attachment(kind="image", path=p, filename=p.name))
                if parts:
                    lines.append("".join(parts))
            text = "\n".join(lines)
        else:
            log_dropped(self.name, user_id, f"nothing readable ({mtype})")
            return None
        if not text.strip() and not attachments:
            log_dropped(self.name, user_id, "nothing readable")
            return None
        return IncomingMessage(channel=self.name, user_id=user_id, text=text.strip(),
                               attachments=attachments, message_id=msg.message_id,
                               reply_to_id=getattr(msg, "parent_id", None) or None)

    def card_to_incoming(self, data) -> IncomingMessage | None:
        value = getattr(data.event.action, "value", None) or {}
        if "fix" not in value:
            return None
        user_id = data.event.operator.open_id
        if not self._is_allowed(user_id):
            return None
        now = time.monotonic()
        self._cards = {k: at for k, at in self._cards.items() if now - at < CARD_DEDUPE_SECONDS}
        if (user_id, value["fix"]) in self._cards:  # double click / redelivery
            return None
        self._cards[(user_id, value["fix"])] = now
        return IncomingMessage(channel=self.name, user_id=user_id, text="", callback=value["fix"])

    async def send(self, user_id: str, reply: Reply) -> SendResult:
        chunks = split_text(reply.text, MAX_TEXT)
        mid = None
        delivered = False
        for i, chunk in enumerate(chunks):
            try:
                if reply.buttons and i == len(chunks) - 1:
                    try:
                        mid = await self.api.send_card(user_id, chunk, reply.buttons, reply.plain)
                    except Exception:
                        log.exception("feishu card send failed; falling back to text")
                        hints = "\n".join(b.fallback for b in reply.buttons if b.fallback)
                        mid = await self.api.send_text(user_id, f"{chunk}\n{hints}" if hints else chunk)
                else:
                    mid = await self.api.send_text(user_id, chunk)
                delivered = True
            except Exception:
                if not delivered:
                    log.exception("feishu send failed")
                    return SendResult(False, reason="error")
                # Earlier chunks are already delivered; reporting failure would make the caller re-send them all.
                log.exception("feishu send partially failed (chunk %d of %d)", i + 1, len(chunks))
                break
        return SendResult(True, mid)

    async def send_image(self, user_id: str, path: Path, caption: str = "") -> SendResult:
        try:
            if caption:
                await self.api.send_text(user_id, caption)
            return SendResult(True, await self.api.send_image(user_id, path))
        except Exception:
            log.exception("feishu send_image failed")
            return SendResult(False)

    async def send_file(self, user_id: str, data: bytes, filename: str) -> SendResult:
        if len(data) > MAX_UPLOAD:
            return SendResult(False, reason="too-big")
        try:
            return SendResult(True, await self.api.send_file(user_id, data, filename))
        except Exception:
            log.exception("feishu send_file failed")
            return SendResult(False, reason="error")

    async def start(self, on_message: OnMessage) -> None:
        if self._ws_thread is not None and self._ws_thread.is_alive():
            raise RuntimeError(t("feishu.still_running"))  # the SDK thread cannot be stopped; never run two
        import lark_oapi as lark
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

        if self.api is None:
            self.api = LarkApi(self.app_id, self.app_secret)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        ws: dict = {}  # filled by the WebSocket thread: client, loop
        stopping = threading.Event()

        def put(item) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:  # our loop is already closed
                log.warning("feishu: dropped an event because the main loop is closed")

        def on_msg(data):  # runs on the WebSocket thread and is acked after it returns: enqueue only
            put(("msg", data))

        def on_card(data):
            put(("card", data))
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": t("feishu.card_ack")}})

        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(on_msg)
                   .register_p2_card_action_trigger(on_card).build())

        def ws_thread():
            import lark_oapi.ws.client as wsc

            ws_loop = wsc.loop = asyncio.new_event_loop()  # lark-oapi keeps a module-level loop; give it its own
            asyncio.set_event_loop(ws_loop)
            try:
                client = lark.ws.Client(self.app_id, self.app_secret, event_handler=handler,
                                        log_level=lark.LogLevel.WARNING)
                ws["client"], ws["loop"] = client, ws_loop
                if stopping.is_set():  # shut down before we even connected: do not start a connection nobody stops
                    return
                client.start()
            except Exception:
                if not stopping.is_set():
                    log.exception("feishu websocket stopped")
            finally:
                try:
                    pending = asyncio.all_tasks(ws_loop)
                    for task in pending:
                        task.cancel()
                    if pending and not ws_loop.is_running():
                        ws_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    ws_loop.close()
                except Exception:
                    log.debug("feishu: could not close the websocket loop", exc_info=True)
            if not stopping.is_set():
                put(("dead", None))  # start() only returns or raises when the connection is gone

        async def watchdog():
            misses = 0
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                client = ws.get("client")
                misses, dead = watchdog_step(client is not None and getattr(client, "_conn", None) is not None, misses)
                if dead:
                    log.warning("feishu: no live connection for %d checks", misses)
                    put(("dead", None))
                    return

        self._ws_thread = threading.Thread(target=ws_thread, daemon=True, name="feishu-ws")
        self._ws_thread.start()
        watchdog_task = asyncio.create_task(watchdog())
        try:
            while True:
                kind, data = await queue.get()
                if kind == "dead":
                    raise RuntimeError(t("feishu.disconnected"))  # `octopus run` restarts the channel with backoff
                try:
                    incoming = await self.to_incoming(data) if kind == "msg" else self.card_to_incoming(data)
                except Exception:
                    log.exception("feishu event handling failed")
                    continue
                if incoming is not None:
                    task = asyncio.create_task(on_message(incoming))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        finally:
            watchdog_task.cancel()
            stopping.set()
            if ws.get("client") is not None:
                await asyncio.to_thread(shutdown_ws, ws["client"], ws["loop"])
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
