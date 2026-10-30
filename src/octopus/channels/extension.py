"""The Chrome extension's local save endpoint (P17): a small HTTP/1.1 server on 127.0.0.1 inside `octopus run`.

POST /save (202 {"id"}) and GET /recent, nothing else. A webpage can send requests to localhost too, so: every request
needs the token (Authorization: Bearer, compared in constant time; a header a page cannot send without a CORS
preflight), CORS is answered for chrome-extension:// origins only, a Host header other than 127.0.0.1:<port> /
localhost:<port> is refused (DNS rebinding) and the body is capped. One request per connection. Nothing of the
token or of the content is logged; what is saved gets its P16 `in` line from the intake.

In a container (P22) it binds 0.0.0.0 (channels.extension.bind) and Docker publishes it as 127.0.0.1:<port>:<port>
on the host: the browser still sends Host 127.0.0.1:<port>, so the Host check is the same."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging

from octopus import host
from octopus.channels.base import OnMessage, Reply, SendResult
from octopus.extension import parse_request
from octopus.i18n import aliases, t

log = logging.getLogger(__name__)
HOST = "127.0.0.1"  # the default bind and the Host header the extension sends
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 15 * 1024 * 1024
MAX_HEAD_BYTES = 64 * 1024
HEAD_TIMEOUT_SECONDS = 10
BODY_TIMEOUT_SECONDS = 60
CLOSE_TIMEOUT_SECONDS = 2  # shutdown: open connections are cut, then this long at most for them to go
ROUTES = {"/save": "POST", "/recent": "GET"}
EXTENSION_ORIGIN = "chrome-extension://"
REASONS = {200: "OK", 202: "Accepted", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
           404: "Not Found", 405: "Method Not Allowed", 411: "Length Required", 413: "Payload Too Large",
           431: "Request Header Fields Too Large", 500: "Internal Server Error", 503: "Service Unavailable"}
__getattr__ = aliases({"SAVE_BROKE": "extension_channel.save_broke", "LIST_BROKE": "extension_channel.list_broke",
                       "NOT_READY": "extension_channel.not_ready", "EXPOSED": "extension_channel.exposed",
                       "PORT_TAKEN": "extension_channel.port_taken"})


class _Refused(Exception):
    def __init__(self, status: int, why: str):
        super().__init__(why)
        self.status = status


class ExtensionChannel:
    name = "extension"
    acks_saves = False

    def __init__(self, token: str, port: int = DEFAULT_PORT, bind: str = HOST):
        self.token = token
        self.port = port
        self.bind = bind
        self._warned = False  # EXPOSED is said once, not on every supervised restart
        self.intake = None  # extension.Intake, set by `octopus run` once the App exists
        self.server: asyncio.base_events.Server | None = None
        self.ready = asyncio.Event()  # bound and listening (tests bind port 0 and read self.port after)
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self, on_message: OnMessage) -> None:
        """Serves until cancelled. Saves never go through on_message: they are not chat."""
        try:
            self.server = await asyncio.start_server(self._client, self.bind, self.port, limit=MAX_HEAD_BYTES)
        except OSError as e:
            raise RuntimeError(t("extension_channel.port_taken", host=self.bind, port=self.port, error=e.strerror or e)) from None
        self.port = self.server.sockets[0].getsockname()[1]
        if self.bind != HOST:
            if not ipaddress.ip_address(self.bind).is_loopback and not host.in_container() and not self._warned:
                self._warned = True  # bind = "0.0.0.0" copied from the Docker setup onto a real machine
                log.warning("%s", t("extension_channel.exposed", bind=self.bind))
            log.info("extension: listening on %s:%d (a container's port, published on the host)", self.bind, self.port)
        self.ready.set()
        try:
            await asyncio.Event().wait()  # serving since start_server; not serve_forever, which waits for every
            # connection when cancelled
        finally:
            self.ready.clear()
            self.server.close()
            for writer in list(self._writers):  # a request still being read would hold shutdown for its timeout
                writer.transport.abort()
            try:
                await asyncio.wait_for(self.server.wait_closed(), CLOSE_TIMEOUT_SECONDS)
            except TimeoutError:
                log.info("extension: connections still open at shutdown, left to close")

    async def send(self, user_id: str, reply: Reply) -> SendResult:
        """A save job's reply: shown by the popup from the database; forwarded to Telegram when asked for."""
        if self.intake is not None:
            await self.intake.reply(user_id, reply.text)
        return SendResult(True)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        origin = None
        self._writers.add(writer)
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_SECONDS)
                method, path, headers = self._parse_head(head)
                origin = headers.get("origin")
                status, payload, extra = await self._answer(reader, method, path, headers)
            except _Refused as e:
                log.info("extension: refused %d (%s)", e.status, e)
                status, payload, extra = e.status, None, []
            except asyncio.LimitOverrunError:
                status, payload, extra = 431, None, []
            except (asyncio.IncompleteReadError, TimeoutError, ConnectionError):
                return  # the other side went away or never finished: nothing to answer
            writer.write(self._response(status, payload, origin, extra))
            await writer.drain()
        except Exception:
            log.exception("extension: answering a request failed")
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    def _parse_head(head: bytes) -> tuple[str, str, dict[str, str]]:
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise _Refused(400, "not an HTTP/1.x request line")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            name = name.strip().lower()
            if not sep or not name:
                raise _Refused(400, "bad header line")
            if name in headers and name in ("host", "authorization", "content-length", "origin"):
                raise _Refused(400, f"two {name} headers")
            headers[name] = value.strip()
        return parts[0], parts[1].split("?", 1)[0], headers

    async def _answer(self, reader: asyncio.StreamReader, method: str, path: str,
                      headers: dict[str, str]) -> tuple[int, dict | None, list[str]]:
        if headers.get("host") not in (f"{HOST}:{self.port}", f"localhost:{self.port}"):
            raise _Refused(403, "foreign Host header")  # DNS rebinding: a page whose name points at 127.0.0.1
        origin = headers.get("origin")
        if origin is not None and not origin.startswith(EXTENSION_ORIGIN):
            raise _Refused(403, "not the extension's origin")  # a webpage: no CORS, no save
        if path not in ROUTES:
            raise _Refused(404, "no such route")
        if method == "OPTIONS":  # the preflight: no token in it, by the CORS rules
            if origin is None:
                raise _Refused(403, "preflight without an origin")
            extra = [f"Access-Control-Allow-Methods: {ROUTES[path]}, OPTIONS",
                     "Access-Control-Allow-Headers: Authorization, Content-Type", "Access-Control-Max-Age: 600"]
            if headers.get("access-control-request-private-network") == "true":
                extra.append("Access-Control-Allow-Private-Network: true")
            return 204, None, extra
        if method != ROUTES[path]:
            raise _Refused(405, "wrong method")
        given = headers.get("authorization", "")
        if not (given.startswith("Bearer ") and hmac.compare_digest(given[7:].encode(), self.token.encode())):
            raise _Refused(401, "missing or wrong token")
        if self.intake is None:
            return 503, {"error": t("extension_channel.not_ready")}, []
        if method == "GET":
            try:
                return 200, await self.intake.recent(), []
            except Exception:
                log.exception("extension: listing the recent saves failed")
                return 500, {"error": t("extension_channel.list_broke")}, []
        return await self._save(reader, headers)

    async def _save(self, reader: asyncio.StreamReader, headers: dict[str, str]) -> tuple[int, dict | None, list[str]]:
        length = headers.get("content-length")
        if length is None or "transfer-encoding" in headers:
            raise _Refused(411, "no Content-Length")
        if not length.isascii() or not length.isdecimal() or len(length) > 10:
            raise _Refused(400, "bad Content-Length")
        if int(length) > MAX_BODY_BYTES:
            await self._discard(reader, int(length))  # read, so the browser sees the 413 and not a reset
            raise _Refused(413, f"{int(length)} bytes")
        body = await asyncio.wait_for(reader.readexactly(int(length)), BODY_TIMEOUT_SECONDS)
        try:
            req = await asyncio.to_thread(parse_request, body)  # a 15 MB page: decoding stays off the event loop
        except ValueError as e:
            log.info("extension: refused 400 (%s)", e)
            return 400, {"error": str(e)}, []
        try:
            rid = await self.intake.save(req)
        except Exception:
            log.exception("extension: saving a request failed")
            return 500, {"error": t("extension_channel.save_broke")}, []
        return 202, {"id": rid}, []

    @staticmethod
    async def _discard(reader: asyncio.StreamReader, n: int) -> None:
        n = min(n, 4 * MAX_BODY_BYTES)
        try:
            while n > 0 and (chunk := await asyncio.wait_for(reader.read(min(n, 1 << 20)), 5)):
                n -= len(chunk)
        except (TimeoutError, ConnectionError):
            pass

    @staticmethod
    def _response(status: int, payload: dict | None, origin: str | None, extra: list[str]) -> bytes:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        lines = [f"HTTP/1.1 {status} {REASONS[status]}", f"Content-Length: {len(body)}", "Connection: close",
                 "Cache-Control: no-store", *extra]
        if body:
            lines.append("Content-Type: application/json; charset=utf-8")
        if origin is not None and origin.startswith(EXTENSION_ORIGIN):  # the extension may read every answer
            lines += [f"Access-Control-Allow-Origin: {origin}", "Vary: Origin"]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
