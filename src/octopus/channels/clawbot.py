"""WeChat ClawBot channel: reply-only, rate-limited, re-login by QR."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Container

import qrcode

from octopus import host
from octopus.channels.base import (OnMessage, Reply, SendResult, log_dropped, out_line, safe_filename, send_status,
                                   split_text, user_tag)
from octopus.channels.ilink import ILINK_BASE, STALE_TOKEN, ILinkClient, ILinkError, LoginResult
from octopus.i18n import aliases, t
from octopus.models import Attachment, IncomingMessage, attachment_from_dict, attachment_to_dict

log = logging.getLogger(__name__)
MAX_TEXT = 2000
SEND_BUDGET = 3
SEND_WINDOW_SECONDS = 480
VOICE_FILENAME = "voice.silk"
POLL_ERROR_PAUSE = 2.0
POLL_ERROR_MAX = 60.0
MIN_POLL_INTERVAL = 1.0
DRAIN_TIMEOUT = 10.0
STALE_PAUSE = 5.0  # at least this long between a -14 and the next connection attempt
RELOGIN_SCHEDULE = [900, 1800, 3600, 7200, 10800]  # waits after a failed QR attempt; the last one repeats
__getattr__ = aliases({"RELOGIN_TEXT": "clawbot.relogin", "VOICE_NO_TEXT": "clawbot.voice_no_text",
                       "MEDIA_FAILED": "clawbot.media_failed", "NOTICE_UNDELIVERED": "clawbot.notice_undelivered"})
TOKEN_SECRET = "clawbot_token"
RECENT_IDS = 200  # message ids handed over lately, kept in the state file: a batch sent again is not handled twice
REPLAY_MAX_AGE = 1800  # a message a stop cut short is handled again on the next start if it came this recently
REPLAY_MAX_TRIES = 2  # ... and at most this many times
INFLIGHT_MAX = 20  # messages kept for a replay at most (the newest): a hung App must not grow the file without end
Notify = Callable[[str, "Path | None"], Awaitable[object]]
_MEDIA = {2: ("image_item", "image", "image.jpg"), 4: ("file_item", "file", "file"),
          5: ("video_item", "video", "video.mp4")}


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


class _LoginRequested(Exception):
    """/clawbot asked for a fresh QR code while the previous one was still waiting to be scanned."""


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        data = {}
    except ValueError:
        log.warning("clawbot state file %s is not valid JSON; starting fresh", path)
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("get_updates_buf"), str):
        data["get_updates_buf"] = ""
    if not isinstance(data.get("context_tokens"), dict):
        data["context_tokens"] = {}
    ids = data.get("recent_ids")
    data["recent_ids"] = [i for i in ids if isinstance(i, str)][-RECENT_IDS:] if isinstance(ids, list) else []
    flight = data.get("inflight")
    data["inflight"] = [i for i in flight if isinstance(i, dict) and isinstance(i.get("id"), str)
                        and isinstance(i.get("at"), (int, float)) and isinstance(i.get("msg"), dict)
                        and isinstance(i.get("tries", 0), int)] if isinstance(flight, list) else []
    return data


def _drop_stale(state: dict, now: float, running: Container[str] = ()) -> tuple[bool, bool]:
    """Drop in-flight messages too old to handle now, and over INFLIGHT_MAX the oldest ones waiting behind another
    message of their user (the first of each user's is the one App is handling now: it stays). running: ids whose
    handler is still at work (no replay for them either, but they are not "dropped"). (Any went, the cap took some.)"""
    flight, keep = state["inflight"], []
    for item in flight:
        age = now - item["at"]
        if age <= REPLAY_MAX_AGE:
            keep.append(item)
        elif item["id"] in running:
            log.info("clawbot: message %s is still being handled after %d min; it will not be handled again after "
                     "a stop", item["id"], age // 60)
        else:
            log.warning("clawbot: message %s came %d min ago; too old to handle now, dropped", item["id"], age // 60)
    capped = len(keep) > INFLIGHT_MAX
    if capped:
        first: dict = {}
        for item in keep:
            first.setdefault(item["msg"].get("user_id"), item["id"])
        waiting = [i["id"] for i in keep if i["id"] not in first.values()]
        cut = set(waiting[:len(keep) - INFLIGHT_MAX])
        keep = [i for i in keep if i["id"] not in cut]
    state["inflight"] = keep
    return len(keep) != len(flight), capped


def _msg_to_dict(m: IncomingMessage) -> dict:
    return {"user_id": m.user_id, "text": m.text, "attachments": [attachment_to_dict(a) for a in m.attachments]}


def _msg_from_dict(d: dict, message_id: str) -> IncomingMessage:
    return IncomingMessage(channel=ClawBotChannel.name, user_id=str(d["user_id"]), text=str(d.get("text") or ""),
                           attachments=[attachment_from_dict(a) for a in d.get("attachments") or []],
                           message_id=message_id)


def _save_state(path: Path, state: dict) -> None:
    """Atomic write; the file holds context tokens, so it is private (0600) from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(state, ensure_ascii=False))
    os.chmod(tmp, 0o600)  # a leftover .tmp keeps its old mode through os.open
    os.replace(tmp, path)


def _write_qr(content: str, qr_path: Path) -> None:
    """The QR image logs a WeChat account into our bot: private (0600) like the state file."""
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = qr_path.with_name(qr_path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        qrcode.make(content).save(fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, qr_path)


def _apply_login(state: dict, res: LoginResult) -> None:
    if state.get("bot_id") and res.bot_id and res.bot_id != state["bot_id"]:
        # Another bot account: the old cursor and reply contexts belong to the old one.
        state["get_updates_buf"] = ""
        state["context_tokens"] = {}
        state["recent_ids"] = []
        state["inflight"] = []
    if res.bot_id:
        state["bot_id"] = res.bot_id
    if res.user_id:
        state["owner_user_id"] = res.user_id
    state["base_url"] = res.base_url


def _store_login(secrets, state_path: Path, state: dict, res: LoginResult) -> None:
    secrets.set(TOKEN_SECRET, res.bot_token)  # the token lives only in Secrets, never in the state file
    _apply_login(state, res)
    _save_state(state_path, state)


def _open_file(path: Path, run=subprocess.run, environ=None) -> None:
    """Shows the QR image: `open` on macOS, `xdg-open` on a Linux desktop; on a server or in Docker nothing opens
    (the path is printed, and the running service sends the code through another channel)."""
    environ = os.environ if environ is None else environ
    if host.system() == host.MAC:
        cmd = "open"
    elif (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")) and shutil.which("xdg-open"):
        cmd = "xdg-open"
    else:
        return
    try:
        run([cmd, str(path)], check=False)
    except OSError:
        pass


async def login_interactive(secrets, state_path: Path, qr_path: Path, client_factory=ILinkClient,
                            print_fn=print, input_fn=input, open_fn=_open_file) -> LoginResult:
    client = client_factory()
    try:
        qr, content = await client.get_qrcode()
        _write_qr(content, qr_path)
        print_fn(t("clawbot.scan", qr_path=qr_path))
        open_fn(qr_path)
        last = [None]

        def on_status(status: str) -> None:
            if status != last[0]:  # the login poll reports every second; print changes only
                last[0] = status
                print_fn(t("clawbot.status", status=status))

        def verify():
            return input_fn(t("clawbot.verify_code")).strip() or None

        res = await client.login(qr, on_status=on_status, verify_code_fn=verify)
    finally:
        await client.close()
    state = _load_state(state_path)
    _drop_stale(state, time.time())  # another process: none of its handlers run here
    _store_login(secrets, state_path, state, res)
    print_fn(t("clawbot.connected"))
    return res


class ClawBotChannel:
    name = "clawbot"
    acks_saves = False

    def __init__(self, secrets, state_path: Path, incoming_dir: Path, qr_path: Path,
                 client_factory=ILinkClient, notify: Notify | None = None, clock: Callable[[], float] = time.monotonic,
                 allowed: Callable[[str], bool] | None = None, wall: Callable[[], float] = time.time):
        self.secrets = secrets
        self.state_path = state_path
        self.incoming_dir = incoming_dir
        self.qr_path = qr_path
        self.client_factory = client_factory
        self.notify = notify
        self.clock = clock
        self.wall = wall  # when a message was accepted: it outlives a restart, so not the monotonic clock
        self.allowed = allowed  # config allowed_users; the owner (who scanned the QR) is always accepted
        self._save_failing = False  # the last state save failed (logged once per streak)
        self._handling: set[str] = set()  # ids of the messages whose handler is running now
        self._capped = False  # over INFLIGHT_MAX now (logged once per streak)
        self._login_unsaved = False  # a re-login's fields are only in memory: the next save writes them
        self.state = self._load()
        self._client = None
        self._client_token: str | None = None  # the token self._client was built with
        self._client_override = None  # tests inject a client for start()
        self._session_token: str | None = None  # a login the Keychain refused to store: kept for this run
        self._stale_token: str | None = None  # expired (-14); a Keychain that would not delete it still returns it
        self._sent: dict[str, deque] = {}
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._login_now = asyncio.Event()  # /clawbot: start a fresh QR attempt now

    @property
    def connected(self) -> bool:
        return self._client is not None

    def request_login(self) -> bool:
        """/clawbot: a fresh QR attempt now (cuts the schedule wait or the current attempt's scan wait short).

        False when connected: there is nothing to scan."""
        if self.connected:
            return False
        self._login_now.set()
        return True

    async def _unless_login_requested(self, aw):
        """aw's result; if /clawbot asks for a fresh QR code first, aw is cancelled and _LoginRequested raised."""
        work = asyncio.ensure_future(aw)
        request = asyncio.ensure_future(self._login_now.wait())
        try:
            await asyncio.wait({work, request}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            request.cancel()
            if not work.done():
                work.cancel()
            await asyncio.gather(work, request, return_exceptions=True)
        if work.cancelled():
            raise _LoginRequested
        return work.result()  # finished first (or together with the request): its result or its error

    def is_owner(self, user_id: str) -> bool:
        return bool(user_id) and user_id == self.state.get("owner_user_id")

    def _accepts(self, user_id: str) -> bool:
        if self.is_owner(user_id):
            return True
        if self.allowed is not None:
            try:
                if self.allowed(user_id):
                    return True
            except Exception:
                log.exception("clawbot allow-list check failed")
        log.info("clawbot: ignoring message from user %s (not the owner; add it to allowed_users to accept)",
                 user_id)
        return False

    async def _connect(self) -> None:
        if self._client_override is not None:
            self._client, self._client_override = self._client_override, None
            self._client_token = getattr(self._client, "token", None)
            return
        # None also when the Keychain is locked: re-login, delete nothing. Keychain calls can block (an
        # access prompt): never on the event loop.
        token = self._session_token or await asyncio.to_thread(self.secrets.get, TOKEN_SECRET)
        if token is not None and token == self._stale_token:
            token = None  # the delete after -14 did not take: this token is dead, log in again
        self._client_token = token
        self._client = (self.client_factory(token=token, base_url=self.state.get("base_url") or ILINK_BASE)
                        if token else None)

    def _load(self) -> dict:
        """The state file, without in-flight messages too old to handle (their text leaves the file now)."""
        state = _load_state(self.state_path)
        if _drop_stale(state, self.wall(), self._handling)[0]:
            self._write(state)
        return state

    def _persist(self) -> None:
        """Save what the running service owns (cursor, reply contexts) into the file as it is on disk now.

        Login fields (owner_user_id, bot_id, base_url) belong to whoever logged in last, e.g. `octopus
        clawbot-login` in another process, so they are never written back from memory."""
        disk = _load_state(self.state_path)
        if (not self._login_unsaved and disk.get("bot_id") and self.state.get("bot_id")
                and disk["bot_id"] != self.state["bot_id"]):
            log.info("clawbot: another bot account logged in meanwhile; not saving the old cursor")
            return
        for key in ("owner_user_id", "bot_id", "base_url"):  # file lost or corrupt: don't lose the login
            if (self._login_unsaved or not disk.get(key)) and self.state.get(key):
                disk[key] = self.state[key]
        disk["get_updates_buf"] = self.state.get("get_updates_buf", "")
        disk["context_tokens"] = self.state["context_tokens"]
        disk["recent_ids"] = self.state.get("recent_ids", [])
        disk["inflight"] = self.state.get("inflight", [])
        if self._write(disk):
            self._login_unsaved = False

    def _write(self, state: dict) -> bool:
        try:
            _save_state(self.state_path, state)
        except OSError as e:  # disk full...: the state stays in memory and the next save writes all of it
            if not self._save_failing:
                log.warning("clawbot: could not save the state file (%s); kept in memory, trying again on the "
                            "next save", e)
            self._save_failing = True
            return False
        if self._save_failing:
            log.info("clawbot: the state file is saved again")
            self._save_failing = False
        return True

    async def _save_file(self, data: bytes, filename: str) -> Path:
        dest = self.incoming_dir / uuid.uuid4().hex
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / filename
        try:
            await asyncio.to_thread(path.write_bytes, data)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return path

    async def to_incoming(self, msg: dict) -> IncomingMessage | None:
        if msg.get("message_type") != 1:
            return None
        user = str(msg.get("from_user_id") or "")
        if not self._accepts(user):  # strangers: no reply context kept, nothing downloaded
            return None
        if msg.get("context_token"):
            self.state["context_tokens"][user] = msg["context_token"]
            self._persist()
        texts, attachments = [], []
        try:
            for item in msg.get("item_list") or []:
                kind_no = item.get("type")
                if kind_no == 1:
                    texts.append((item.get("text_item") or {}).get("text") or "")
                elif kind_no == 3:  # WeChat's own transcript, handled as typed text; SILK audio is not decoded
                    transcript = ((item.get("voice_item") or {}).get("text") or "").strip()
                    if transcript:
                        attachments.append(Attachment(kind="audio", path=None, filename=VOICE_FILENAME, text=transcript,
                                                      voice=True))
                    else:
                        self._spawn(self._tell(user, t("clawbot.voice_no_text")))
                elif kind_no in _MEDIA:
                    key, kind, default = _MEDIA[kind_no]
                    body = item.get(key) or {}
                    try:
                        if self._client is None:
                            raise ILinkError(None, t("clawbot.not_connected"))
                        data = await self._client.download_media(body.get("media") or {}, body.get("aeskey"))
                        path = await self._save_file(data, safe_filename(body.get("file_name"), default))
                    except Exception:
                        log.exception("clawbot media download failed")
                        texts.append(t("clawbot.media_failed"))
                        continue
                    attachments.append(Attachment(kind=kind, path=path, filename=path.name, size=len(data)))
        except BaseException:  # a stop mid-download: the files staged for the earlier items go too (F11)
            for a in attachments:
                if a.path is not None:
                    shutil.rmtree(a.path.parent, ignore_errors=True)  # its own _incoming/<uuid>/ dir
            raise
        text = "\n".join(x for x in texts if x).strip()
        if not text and not attachments:  # the app never sees it: say so, or "did it arrive?" has no answer
            types = [item.get("type") for item in msg.get("item_list") or [] if isinstance(item, dict)]
            log_dropped(self.name, user, f"nothing readable (item types {types})")
            return None
        return IncomingMessage(channel=self.name, user_id=user, text=text, attachments=attachments,
                               message_id=str(msg.get("message_id", "")))

    async def _tell(self, user_id: str, text: str) -> None:
        try:
            result = await self.send(user_id, Reply(text))
            log.info(out_line(self.name, user_id, text, send_status(result)))  # it never passes App._deliver
            if not result.ok:
                log.warning("clawbot: could not tell %s: %s", user_tag(user_id), text)
        except Exception:
            log.exception("clawbot: could not tell %s: %s", user_tag(user_id), text)

    def _budget_left(self, user_id: str) -> int:
        q = self._sent.setdefault(user_id, deque())
        now = self.clock()
        while q and now - q[0] > SEND_WINDOW_SECONDS:
            q.popleft()
        return SEND_BUDGET - len(q)

    def sends_left(self, user_id: str) -> int:
        """How many messages may go to the user right now (/read sends that many parts and says how to go on)."""
        return max(self._budget_left(user_id), 0)

    async def send(self, user_id: str, reply: Reply) -> SendResult:
        ctx = self.state["context_tokens"].get(user_id)
        client = self._client
        if client is None or not ctx:
            return SendResult(False, reason="not-connected" if client is None else "no-context")
        text = reply.text
        hints = [b.fallback for b in reply.buttons if b.fallback]  # e.g. the guide's buttons have none
        if hints:
            text += "\n" + "\n".join(hints)
        chunks = split_text(text, MAX_TEXT)
        last, reason = None, "budget"
        async with self._send_lock:  # budget check and send must not interleave
            left = self._budget_left(user_id)
            if len(chunks) > left and len(chunks) <= SEND_BUDGET:
                # it would be cut off: send none of it, App keeps the whole reply in the outbox
                return SendResult(False, reason="budget")
            if len(chunks) > SEND_BUDGET:
                log.warning("clawbot reply has %d parts, more than the %d sends allowed per %d min: "
                            "only the first %d are sent", len(chunks), SEND_BUDGET, SEND_WINDOW_SECONDS // 60,
                            max(left, 0))
            for i, chunk in enumerate(chunks):
                if self._budget_left(user_id) <= 0:
                    break
                try:
                    mid = await client.send_text(user_id, chunk, ctx)
                except Exception:
                    log.exception("clawbot send failed (chunk %d of %d)", i + 1, len(chunks))
                    reason = "error"
                    break
                self._sent[user_id].append(self.clock())  # the server accepted it, so it counts
                if not mid:  # "fake success": accepted but not delivered
                    log.warning("clawbot sendmessage returned no message_id (chunk %d of %d)", i + 1, len(chunks))
                    reason = "error"
                    break
                last = mid
        # Delivered chunks are never re-sent: a partial delivery is reported as sent.
        return SendResult(True, last) if last else SendResult(False, reason=reason)

    async def _notify_owner(self) -> None:
        delivered = 0
        if self.notify is not None:
            try:
                delivered = await self.notify(t("clawbot.relogin"), self.qr_path)
            except Exception:
                log.exception("clawbot could not send the re-login notice")
        if not delivered:
            log.warning(t("clawbot.notice_undelivered", qr_path=self.qr_path))

    async def relogin(self) -> None:
        """One QR attempt: a fresh QR code, one notice, then wait for the scan (the code lives ~8 minutes).

        Deletes nothing: the caller decides whether the old token is stale. Raises _LoginRequested when
        /clawbot asks for a fresh code before this one was scanned."""
        self._login_now.clear()  # this attempt is the fresh code any earlier request asked for
        client = self.client_factory()
        try:
            qr, content = await client.get_qrcode()
            _write_qr(content, self.qr_path)
            await self._notify_owner()
            res = await self._unless_login_requested(client.login(qr))
        finally:
            await client.close()
        self._stale_token = None  # a scan just produced this login: it is not the one that expired
        try:
            await asyncio.to_thread(self.secrets.set, TOKEN_SECRET, res.bot_token)
            self._session_token = None
        except Exception as e:  # don't lose a successful scan: keep the token in memory for this run
            log.error("clawbot: could not save the new login to the Keychain (%s); it lasts until Octopus stops",
                      type(e).__name__)
            self._session_token = res.bot_token
        state = dict(self.state) if self._save_failing else self._load()  # the newer one
        _apply_login(state, res)
        self._login_unsaved = not self._write(state)
        self.state = state
        log.info("clawbot re-login complete")

    def _spawn(self, coro, mid: str = "") -> None:
        """mid: the message the task handles, counted as being handled from now until the task ends."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if mid:
            self._handling.add(mid)
            task.add_done_callback(lambda _: self._handling.discard(mid))

    async def _drain(self, timeout: float | None = None) -> None:
        if not self._tasks:
            return
        tasks = list(self._tasks)
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            log.warning("clawbot: cancelling %d unfinished message task(s)", len(pending))
            for task in pending:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle(self, on_message: OnMessage, incoming: IncomingMessage, mid: str) -> None:
        """Hand the message to App; only once that finished is it done. A stop (cancellation, or a kill) before
        then leaves it in "inflight": the next start handles it again."""
        try:
            await on_message(incoming)
        except Exception:  # App answers its own errors; never handle a message that breaks it again and again
            log.exception("clawbot: handling message %s failed", mid)
        if mid:
            self.state["inflight"] = [i for i in self.state["inflight"] if i["id"] != mid]
            self._persist()

    def _replay(self, on_message: OnMessage) -> None:
        """Messages a stop cut short: handled again, in order, unless they came over REPLAY_MAX_AGE ago or were
        handled again REPLAY_MAX_TRIES times already (their staged files are left to App's startup sweep)."""
        _drop_stale(self.state, self.wall(), self._handling)  # a re-login may have taken long
        flight, keep = self.state["inflight"], []
        if not flight:
            return
        for item in flight:
            tries = item.get("tries", 0)
            if tries >= REPLAY_MAX_TRIES:
                log.warning("clawbot: message %s was handled again %d times and cut short each time; dropped",
                            item["id"], tries)
                continue
            try:
                incoming = _msg_from_dict(item["msg"], item["id"])
            except Exception:
                log.exception("clawbot: message %s was cut short by a stop and cannot be read back; dropped",
                              item["id"])
                continue
            log.info("clawbot: message %s was cut short by a stop; handling it again", item["id"])
            item["tries"] = tries + 1
            keep.append(item)
            self._spawn(self._handle(on_message, incoming, item["id"]), item["id"])  # runs after the save below
        self.state["inflight"] = keep
        self._persist()

    async def _poll(self, on_message: OnMessage) -> None:
        loop = asyncio.get_running_loop()
        delay = POLL_ERROR_PAUSE
        while True:
            started = loop.time()
            try:
                msgs, buf, _ = await self._client.get_updates(self.state.get("get_updates_buf", ""))
            except ILinkError as e:
                if e.code == STALE_TOKEN:
                    raise
                log.warning("clawbot getupdates error %s; retrying in %.0f s", e, delay)
                await _pause(delay)
                delay = min(delay * 2, POLL_ERROR_MAX)
                continue
            delay = POLL_ERROR_PAUSE
            if msgs:
                log.debug("clawbot: %d update(s)", len(msgs))
            for m in msgs:
                mid = str(m.get("message_id") or "") if isinstance(m, dict) else ""
                if mid and mid in self.state["recent_ids"]:  # sent again after a stop mid-batch (cursor kept)
                    log.debug("clawbot: message %s handed over before, skipped", mid)
                    continue
                try:
                    incoming = await self.to_incoming(m)
                except Exception:
                    log.exception("clawbot: could not read incoming message")
                    incoming = None
                if incoming is not None:
                    if mid:  # accepted: a stop before App is done with it gets it handled on the next start
                        self.state["inflight"].append({"id": mid, "at": self.wall(), "msg": _msg_to_dict(incoming)})
                        capped = _drop_stale(self.state, self.wall(), self._handling)[1]
                        if capped and not self._capped:
                            log.warning("clawbot: more than %d messages in flight; those waiting longest behind "
                                        "another are not handled again after a stop", INFLIGHT_MAX)
                        self._capped = capped
                    self._spawn(self._handle(on_message, incoming, mid), mid)  # return fast; App orders per user
                if mid:  # handed over, or nothing to hand over: never from the server again
                    self.state["recent_ids"] = (self.state["recent_ids"] + [mid])[-RECENT_IDS:]
                    self._persist()
            # the new cursor only once the whole batch is handed over: a stop while a message's files are still
            # downloading gets the batch again on the next start (to_incoming's own saves keep the old cursor)
            if buf != self.state.get("get_updates_buf"):
                self.state["get_updates_buf"] = buf
                self._persist()
            if not msgs:
                elapsed = loop.time() - started
                if elapsed < MIN_POLL_INTERVAL:  # a server answering empty polls instantly must not spin us
                    await _pause(MIN_POLL_INTERVAL - elapsed)

    async def start(self, on_message: OnMessage) -> None:
        failures = 0
        replay = True
        try:
            while True:
                if not self._save_failing:  # else memory is newer than the file
                    self.state = self._load()  # `octopus clawbot-login` may have changed it
                await self._connect()
                if self._client is None:
                    try:
                        await self.relogin()
                        failures = 0
                    except _LoginRequested:
                        log.info("clawbot: a fresh QR code was requested; starting a new attempt")
                    except ILinkError as e:  # QR expired unscanned, network...: a fresh QR + notice later
                        wait = RELOGIN_SCHEDULE[min(failures, len(RELOGIN_SCHEDULE) - 1)]
                        failures += 1
                        log.warning("clawbot re-login failed (%s); next QR code in %d min", e, wait // 60)
                        try:
                            await self._unless_login_requested(_pause(wait))
                        except _LoginRequested:  # /clawbot: no more waiting
                            log.info("clawbot: a fresh QR code was requested; not waiting %d min", wait // 60)
                    continue
                if replay:  # once per start, connected (replies go out at once), before anything new
                    replay = False
                    self._replay(on_message)
                try:
                    await self._poll(on_message)
                except ILinkError as e:
                    if e.code != STALE_TOKEN:
                        raise
                    log.warning("clawbot session expired (%s)", STALE_TOKEN)
                await self._drain()  # in-flight replies finish (or fail into the outbox) before the client goes
                await self._client.close()
                self._client = None
                self._stale_token = self._client_token
                if self._session_token == self._client_token:
                    self._session_token = None
                if await asyncio.to_thread(self.secrets.get, TOKEN_SECRET) == self._client_token:
                    try:
                        await asyncio.to_thread(self.secrets.delete, TOKEN_SECRET)
                    except Exception as e:  # _stale_token keeps us from reusing it anyway
                        log.warning("clawbot: could not delete the expired token from the Keychain (%s)",
                                    type(e).__name__)
                else:  # logged in again meanwhile (e.g. `octopus clawbot-login`): keep the new token
                    log.info("clawbot token changed meanwhile; reconnecting with the new one")
                await _pause(STALE_PAUSE)  # never a tight reconnect loop, whatever the Keychain does
        finally:
            await self._drain(DRAIN_TIMEOUT)
            if self._client is not None:
                await self._client.close()
                self._client = None
