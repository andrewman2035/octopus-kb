"""Telegram bot channel: long polling inside our event loop (python-telegram-bot 22)."""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import Callable

from octopus.channels.base import (Button, OnMessage, Reply, SendResult, log_dropped, safe_filename, split_text,
                                   tell_dropped, user_tag)
from octopus.i18n import aliases, t
from octopus.models import Attachment, IncomingMessage

log = logging.getLogger(__name__)
MAX_TEXT = 4096
MAX_DOWNLOAD = 20_000_000
MAX_UPLOAD = 50_000_000  # what a bot may send as a document
READ_TIMEOUT_SECONDS = 300  # one message's downloads at most this long: the user's next messages wait for it
# the menu next to the input box (tappable commands), in this order
MENU_COMMANDS = ("s", "recent", "list", "read", "find", "delete", "resummarize", "done", "model", "status", "clawbot",
                 "help")


def command_menu() -> list[tuple[str, str]]:
    """(command, description) for Telegram's menu, in the configured language."""
    return [(c, t(f"telegram.menu.{c}")) for c in MENU_COMMANDS]


__getattr__ = aliases({"TOO_BIG": "telegram.too_big", "DOWNLOAD_FAILED": "channels.download_failed",
                       "TOKEN_REJECTED": "telegram.token_rejected", "COMMAND_MENU": command_menu})
ROW_CHARS = 14  # buttons share a row while their labels fit on a phone screen
ROW_BUTTONS = 3
KEEP_KEYBOARD = ("cmd:", "read:")  # the guide's and /read's buttons stay usable after a tap; others are used up


def button_rows(buttons: list[Button]) -> list[list[Button]]:
    rows: list[list[Button]] = []
    width = 0
    for b in buttons:
        if rows and len(rows[-1]) < ROW_BUTTONS and width + len(b.label) <= ROW_CHARS:
            rows[-1].append(b)
            width += len(b.label)
        else:
            rows.append([b])
            width = len(b.label)
    return rows


class TelegramChannel:
    name = "telegram"
    acks_saves = True

    def __init__(self, token: str, incoming_dir: Path, bot=None, allowed: Callable[[str], bool] | None = None):
        self.token = token
        self.allowed = allowed  # checked before anything is downloaded or sent
        self.incoming_dir = incoming_dir
        self.bot = bot
        self._tasks: set[asyncio.Task] = set()
        self._last: dict[str | None, asyncio.Task] = {}  # per user: the latest message still being read

    def _is_allowed(self, user_id: str) -> bool:
        if self.allowed is None:
            return True
        try:
            ok = bool(self.allowed(user_id))
        except Exception:
            log.exception("telegram allow-list check failed")
            ok = False
        if not ok:
            log.info("telegram: ignoring message from user %s (add it to allowed_users to accept)", user_id)
        return ok

    def _scrub(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    async def _download(self, obj, filename: str, mime: str | None = None) -> Path:
        name = safe_filename(filename, "file")
        if not Path(name).suffix and mime:
            name += mimetypes.guess_extension(mime.split(";")[0].strip()) or ""
        dest = self.incoming_dir / uuid.uuid4().hex
        dest.mkdir(parents=True, exist_ok=True)
        try:
            path = dest / name
            f = await self.bot.get_file(obj)
            await f.download_to_drive(custom_path=path)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return path

    @staticmethod
    def _discard(attachments: list[Attachment]) -> None:
        for a in attachments:
            if a.path is not None:
                shutil.rmtree(a.path.parent, ignore_errors=True)  # the per-message unique staging dir

    async def to_incoming(self, m) -> IncomingMessage | None:
        if getattr(m.chat, "type", "private") != "private" or m.from_user is None:
            return None
        user_id = str(m.from_user.id)
        if not self._is_allowed(user_id):
            return None
        text = (m.text or m.caption or "").strip()
        candidates = []
        if m.photo:
            candidates.append(("image", m.photo[-1], "photo.jpg", False))
        for attr, kind, default in (("voice", "audio", "voice.ogg"), ("audio", "audio", "audio"),
                                    ("video", "video", "video.mp4"), ("video_note", "video", "video_note.mp4"),
                                    ("document", "file", "file")):
            obj = getattr(m, attr, None)
            if obj is not None:
                # only a hold-to-talk voice message is handled as spoken words; audio files are saved
                candidates.append((kind, obj, getattr(obj, "file_name", None) or default, attr == "voice"))
        attachments: list[Attachment] = []
        try:
            for kind, obj, filename, is_voice in candidates:
                if (getattr(obj, "file_size", 0) or 0) > MAX_DOWNLOAD:
                    self._discard(attachments)
                    await tell_dropped(self, user_id, "too big", t("telegram.too_big"))
                    return None
                mime = getattr(obj, "mime_type", None)
                path = await self._download(obj, filename, mime)
                attachments.append(Attachment(kind=kind, path=path, filename=path.name, mime=mime,
                                              size=getattr(obj, "file_size", None), voice=is_voice))
        except Exception as e:
            self._discard(attachments)
            log.warning("telegram attachment download failed: %s: %s", type(e).__name__, self._scrub(str(e)))
            too_big = "too big" in str(e).lower()
            await tell_dropped(self, user_id, "too big" if too_big else "download failed",
                               t("telegram.too_big" if too_big else "channels.download_failed"))
            return None
        except BaseException:
            self._discard(attachments)
            raise
        if not text and not attachments:  # a sticker, a location …: App never sees it
            log_dropped(self.name, user_id, "nothing readable")
            return None
        reply_to = m.reply_to_message.message_id if m.reply_to_message else None
        return IncomingMessage(channel=self.name, user_id=user_id, text=text, attachments=attachments,
                               message_id=str(m.message_id), reply_to_id=str(reply_to) if reply_to else None)

    async def on_callback(self, q) -> IncomingMessage | None:
        if q.from_user is None or not q.data or not self._is_allowed(str(q.from_user.id)):
            return None
        try:
            await q.answer()
        except Exception:  # expired query: harmless
            log.info("could not answer callback", exc_info=True)
        if not q.data.startswith(KEEP_KEYBOARD):
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:  # message too old to edit: harmless
                log.info("could not remove keyboard", exc_info=True)
        return IncomingMessage(channel=self.name, user_id=str(q.from_user.id), text="", callback=q.data)

    async def send(self, user_id: str, reply: Reply) -> SendResult:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        chunks = split_text(reply.text, MAX_TEXT)
        markup = (InlineKeyboardMarkup([[InlineKeyboardButton(b.label, callback_data=b.data) for b in row]
                                        for row in button_rows(reply.buttons)])
                  if reply.buttons else None)
        last = None
        for i, chunk in enumerate(chunks):
            try:
                last = await self.bot.send_message(int(user_id), chunk,
                                                   reply_markup=markup if i == len(chunks) - 1 else None)
            except Exception:
                if last is None:
                    log.exception("telegram send failed")
                    return SendResult(False, reason="error")
                # Earlier chunks are already delivered; reporting failure would make the caller re-send them all.
                log.exception("telegram send partially failed (chunk %d of %d)", i + 1, len(chunks))
                break
        return SendResult(True, str(last.message_id) if last is not None else None)

    async def send_image(self, user_id: str, path: Path, caption: str = "") -> SendResult:
        try:
            with open(path, "rb") as fh:
                sent = await self.bot.send_photo(int(user_id), photo=fh, caption=caption)
        except Exception:
            log.exception("telegram send_photo failed")
            return SendResult(False)
        return SendResult(True, str(sent.message_id))

    async def send_file(self, user_id: str, data: bytes, filename: str) -> SendResult:
        if len(data) > MAX_UPLOAD:
            return SendResult(False, reason="too-big")
        try:
            sent = await self.bot.send_document(int(user_id), document=data, filename=filename)
        except Exception:
            log.exception("telegram send_document failed")
            return SendResult(False, reason="error")
        return SendResult(True, str(sent.message_id))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle_message(self, update, on_message: OnMessage) -> None:
        """Returns at once (updates come one at a time): the message is read (its files downloaded) in the
        background, but handed to App only after the same user's previous message was, so a text sent after a
        slow photo is never routed before it. Other users never wait for each other."""
        m = update.effective_message
        sender = getattr(m, "from_user", None)
        user_id = str(sender.id) if sender is not None else None
        before = self._last.get(user_id)

        async def read_then_hand_over() -> None:
            try:
                incoming = await asyncio.wait_for(self.to_incoming(m), READ_TIMEOUT_SECONDS)
            except TimeoutError:  # to_incoming removed what it had staged when it was cancelled
                log.warning("telegram: reading a message from %s took over %s s, given up",
                            user_tag(user_id) if user_id else "-", READ_TIMEOUT_SECONDS)
                incoming = None
                if user_id is not None:
                    await tell_dropped(self, user_id, "read timed out", t("channels.download_failed"))
            except Exception:
                log.exception("telegram: could not read incoming message")
                incoming = None
            if before is not None:
                await asyncio.wait({before})  # never raises; a failed read before this one is just skipped
            if incoming is not None:
                self._spawn(on_message(incoming))  # App takes them in this order (its per-user lock is FIFO)

        task = asyncio.create_task(read_then_hand_over())
        self._tasks.add(task)
        self._last[user_id] = task

        def done(done_task: asyncio.Task) -> None:
            self._tasks.discard(done_task)
            if self._last.get(user_id) is done_task:
                del self._last[user_id]

        task.add_done_callback(done)

    async def handle_callback(self, update, on_message: OnMessage) -> None:
        """A tap answers something already shown and downloads nothing: it never waits for a message still
        being read."""
        async def read_then_hand_over() -> None:
            try:
                incoming = await self.on_callback(update.callback_query)
            except Exception:
                log.exception("telegram: could not read button press")
                return
            if incoming is not None:
                await on_message(incoming)

        self._spawn(read_then_hand_over())

    async def drain(self) -> None:
        """Wait for every message being read or handled, including those handed over meanwhile."""
        while running := [task for task in self._tasks if not task.done()]:  # a done one may not be discarded yet
            await asyncio.gather(*running, return_exceptions=True)

    async def _shutdown(self, app) -> None:
        try:
            if app.updater.running:
                await app.updater.stop()
        finally:
            try:
                if app.running:
                    await app.stop()
            finally:
                await self.drain()

    async def _set_command_menu(self, bot) -> None:
        from telegram import BotCommand

        try:
            await bot.set_my_commands([BotCommand(c, d) for c, d in command_menu()])
        except Exception as e:  # the commands still work when typed
            log.warning("telegram: could not set the command menu: %s: %s", type(e).__name__, self._scrub(str(e)))

    async def start(self, on_message: OnMessage) -> None:
        from telegram.error import InvalidToken
        from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

        # httpx logs request URLs at INFO and python-telegram-bot logs its API URL at DEBUG; both contain the token
        for noisy, level in (("httpx", logging.WARNING), ("httpcore", logging.WARNING), ("telegram", logging.INFO)):
            logging.getLogger(noisy).setLevel(level)
        # one update at a time, in order: the handlers only start the work, so a slow download holds up nothing
        app = Application.builder().token(self.token).build()
        self.bot = app.bot

        async def on_msg(update, context):
            await self.handle_message(update, on_message)

        async def on_btn(update, context):
            await self.handle_callback(update, on_message)

        app.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, on_msg))
        app.add_handler(CallbackQueryHandler(on_btn))
        try:
            async with app:
                await app.start()
                try:
                    await self._set_command_menu(app.bot)
                    await app.updater.start_polling(allowed_updates=["message", "callback_query"])
                    await asyncio.Event().wait()  # until cancelled by `octopus run`
                finally:
                    await self._shutdown(app)
        except InvalidToken:
            raise RuntimeError(t("telegram.token_rejected")) from None  # PTB puts the token in this error's text
        except Exception as e:
            if self.token and self.token in str(e):
                raise RuntimeError(t("telegram.connect_failed")) from None
            raise
