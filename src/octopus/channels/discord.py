"""Discord bot channel (P23): direct messages from the owner over the gateway (discord.py), no public URL.

Behaves like the Telegram channel: text is chat, links / files / images are saved, /commands are typed, a voice
message goes the voice path. Only DMs from owner_user_ids are read; anything else is ignored with one log line per
user. Buttons are message components whose custom_id is the Button's data: a tap is handled from the raw interaction,
so a button sent before a restart still works. The /commands are also registered as Discord's own (the "/" menu), and
picking one is handed over as if its line had been typed (P30)."""
from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from opencc import OpenCC

from octopus.channels.base import (Button, OnMessage, Reply, SendResult, log_dropped, safe_filename, split_text,
                                   tell_dropped, user_tag)
from octopus.i18n import aliases, t, text_in
from octopus.models import Attachment, IncomingMessage

log = logging.getLogger(__name__)
MAX_TEXT = 2000  # Discord's limit per message
MAX_DOWNLOAD = 100_000_000
MAX_UPLOAD = 10_000_000  # a bot's attachment in a DM (servers without boosts)
MAX_BUTTONS = 25  # 5 rows of 5
MAX_LABEL = 80
MAX_CUSTOM_ID = 100
READ_TIMEOUT_SECONDS = 300
__getattr__ = aliases({"TOO_BIG": "discord.too_big", "DOWNLOAD_FAILED": "channels.download_failed",
                       "TOKEN_REJECTED": "discord.token_rejected"})
KEEP_BUTTONS = ("cmd:", "read:")  # the guide's and /read's buttons stay usable after a tap; others are used up
# An idle HTTPS connection to Discord is kept this long (aiohttp closes it after 15 s): through a local proxy a new
# one took 2-5 s, more than the 3 s Discord gives a tap's answer (P30). Nothing is sent to keep it. Measured through
# that proxy: still good after 240 s idle, silently dead after 400 s (a request on it hung for a minute), so well
# under 240 s.
KEEP_ALIVE_SECONDS = 180
# Discord takes no answer after 3 s; one stuck on a dead connection must not hold the tap (and the user's next
# messages, queued behind it) for the minute it can take to fail
ACK_TIMEOUT_SECONDS = 5
# Discord's "/" menu: (name, its text after it: "required", "optional" or None); the descriptions are
# discord.cmd.<name> in both catalogs
SLASH_COMMANDS = (("s", "required"), ("add", "required"), ("new", None), ("done", None), ("recent", "optional"),
                  ("list", "optional"), ("read", "required"), ("find", "required"), ("delete", "required"),
                  ("resummarize", "required"), ("model", "optional"), ("status", None), ("clawbot", None),
                  ("help", None))
BOT_DM, GUILD_INSTALL, CHAT_INPUT, STRING = 1, 0, 1, 3  # Discord's numbers
_S2TW = OpenCC("s2tw").convert  # zh-TW: the Chinese catalog in traditional characters, as the extension's zh_TW


def _chinese(key: str) -> dict:
    zh = text_in("zh", key)
    return {"zh-CN": zh, "zh-TW": _S2TW(zh)}


def _described(key: str) -> dict:
    return {"description": text_in("en", key), "description_localizations": _chinese(key)}


def command_payload() -> list[dict]:
    """The commands as Discord takes them: usable in DMs with the bot only, the names as typed in both languages."""
    payload = []
    for name, text in SLASH_COMMANDS:
        c = {"name": name, "type": CHAT_INPUT, **_described(f"discord.cmd.{name}"), "contexts": [BOT_DM],
             "integration_types": [GUILD_INSTALL]}
        if text:
            c["options"] = [{"name": "text", "name_localizations": _chinese("discord.cmd_text_name"),
                             "type": STRING, "required": text == "required", **_described("discord.cmd_text")}]
        payload.append(c)
    return payload


def _shape(commands: list[dict]) -> list[dict]:
    """What a command list says, without what Discord adds (ids, versions, nulls, an omitted required)."""
    def option(o: dict) -> dict:
        return {"name": o.get("name"), "type": o.get("type"), "required": bool(o.get("required")),
                "description": o.get("description"), "name_localizations": o.get("name_localizations") or {},
                "description_localizations": o.get("description_localizations") or {}}
    return sorted(({"name": c.get("name"), "type": c.get("type", CHAT_INPUT), "description": c.get("description"),
                    "description_localizations": c.get("description_localizations") or {},
                    "contexts": sorted(c.get("contexts") or []),
                    "integration_types": sorted(c.get("integration_types") or []),
                    "options": [option(o) for o in c.get("options") or []]} for c in commands),
                  key=lambda c: c["name"] or "")


def _kind(content_type: str | None) -> str:
    major = (content_type or "").split("/")[0].lower()
    return {"image": "image", "audio": "audio", "video": "video"}.get(major, "file")


class DiscordChannel:
    name = "discord"
    acks_saves = True

    def __init__(self, token: str, incoming_dir: Path, client=None, allowed: Callable[[str], bool] | None = None):
        self.token = token
        self.incoming_dir = incoming_dir
        self.client = client
        self.allowed = allowed
        self._ignored: set[str] = set()  # users told about once in the log
        self._server_logged: set[str] = set()  # owners whose server message was logged once
        self._tasks: set[asyncio.Task] = set()
        self._last: dict[str, asyncio.Task] = {}  # per user: the latest message still being read
        self._commands_synced = False

    def _is_allowed(self, user_id: str) -> bool:
        if self.allowed is None:
            return True
        try:
            ok = bool(self.allowed(user_id))
        except Exception:
            log.exception("discord owner check failed")
            ok = False
        if not ok and user_id not in self._ignored:
            self._ignored.add(user_id)
            log.info("in discord %s ignored: not an owner (add the id to owner_user_ids to accept)",
                     user_tag(user_id))
        return ok

    def _quiet_allowed(self, user_id: str) -> bool:
        try:
            return bool(self.allowed(user_id))
        except Exception:
            return False

    @staticmethod
    def _discard(attachments: list[Attachment]) -> None:
        for a in attachments:
            if a.path is not None:
                shutil.rmtree(a.path.parent, ignore_errors=True)  # the per-file unique staging dir

    async def _download(self, att) -> Path:
        name = safe_filename(att.filename, "file")
        if not Path(name).suffix and att.content_type:
            name += mimetypes.guess_extension(att.content_type.split(";")[0].strip()) or ""
        dest = self.incoming_dir / uuid.uuid4().hex
        dest.mkdir(parents=True, exist_ok=True)
        try:
            await att.save(dest / name)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return dest / name

    async def to_incoming(self, m) -> IncomingMessage | None:
        if getattr(m.author, "bot", False):  # our own messages, other bots
            return None
        user_id = str(m.author.id)
        if m.guild is not None:  # only DMs are read (with minimal intents, server messages do not even come)
            if user_id not in self._server_logged and self.allowed is not None and self._quiet_allowed(user_id):
                self._server_logged.add(user_id)
                log.info("in discord %s ignored: a server message (only direct messages are read)", user_tag(user_id))
            return None
        if not self._is_allowed(user_id):
            return None
        text = (m.content or "").strip()
        attachments: list[Attachment] = []
        try:
            for att in m.attachments:
                if (att.size or 0) > MAX_DOWNLOAD:
                    self._discard(attachments)
                    await tell_dropped(self, user_id, "too big", t("discord.too_big"))
                    return None
                path = await self._download(att)
                attachments.append(Attachment(kind=_kind(att.content_type), path=path, filename=path.name,
                                              mime=att.content_type, size=att.size,
                                              voice=bool(att.is_voice_message())))
        except Exception as e:
            self._discard(attachments)
            log.warning("discord attachment download failed: %s", type(e).__name__)
            await tell_dropped(self, user_id, "download failed", t("channels.download_failed"))
            return None
        except BaseException:
            self._discard(attachments)
            raise
        if not text and not attachments:  # a sticker, an embed only …: App never sees it
            log_dropped(self.name, user_id, "nothing readable")
            return None
        ref = getattr(m, "reference", None)
        reply_to = getattr(ref, "message_id", None) if ref is not None else None
        return IncomingMessage(channel=self.name, user_id=user_id, text=text, attachments=attachments,
                               message_id=str(m.id), reply_to_id=str(reply_to) if reply_to else None)

    async def on_callback(self, interaction) -> IncomingMessage | None:
        data = getattr(interaction, "data", None) or {}
        custom_id = data.get("custom_id")
        user = getattr(interaction, "user", None)
        if getattr(interaction, "guild_id", None) is not None:  # buttons are only ever sent in DMs
            return None
        if not custom_id or user is None or not self._is_allowed(str(user.id)):
            return None
        if custom_id.startswith(KEEP_BUTTONS):
            await self._answer(interaction, f"a tap from {user_tag(str(user.id))} ({custom_id})",
                               interaction.response.defer)
        else:  # answers the tap and removes the buttons
            await self._answer(interaction, f"a tap from {user_tag(str(user.id))} ({custom_id})",
                               lambda: interaction.response.edit_message(view=None))
        return IncomingMessage(channel=self.name, user_id=str(user.id), text="", callback=custom_id)

    async def on_command(self, interaction) -> IncomingMessage | None:
        """A command picked from Discord's "/" menu: answered with its line (silently), then that line is what App
        gets, as if typed."""
        data = getattr(interaction, "data", None) or {}
        name = data.get("name")
        user = getattr(interaction, "user", None)
        if getattr(interaction, "guild_id", None) is not None:  # registered for DMs with the bot only
            return None
        if not name or user is None or not self._is_allowed(str(user.id)):
            return None
        text = next((str(o.get("value") or "") for o in data.get("options") or [] if o.get("name") == "text"), "")
        line = f"/{name} {text.strip()}".rstrip()
        echo = line if len(line) <= MAX_TEXT else line[:MAX_TEXT - 1] + "…"
        await self._answer(interaction, f"/{name} from {user_tag(str(user.id))}",
                           lambda: interaction.response.send_message(echo, silent=True))
        return IncomingMessage(channel=self.name, user_id=str(user.id), text=line, message_id=str(interaction.id))

    @staticmethod
    async def _answer(interaction, what: str, answer) -> None:
        """Discord's 3 s for the answer count from when the interaction was made: the line says how old it was when
        answered and how long answering took, so a late one tells late delivery from a slow answer. Failing (an old
        message, a tap already answered, too late) changes nothing else."""
        created = getattr(interaction, "created_at", None)
        age = f"{(datetime.now(timezone.utc) - created).total_seconds():.1f}" if created is not None else "?"
        started = time.monotonic()
        try:
            await asyncio.wait_for(answer(), ACK_TIMEOUT_SECONDS)
        except Exception as e:
            log.info("discord: could not answer %s: %s: %s; answered %s s after it was made, the answer took %.1f s",
                     what, type(e).__name__, e, age, time.monotonic() - started)
        else:
            log.debug("discord: answered %s %s s after it was made, the answer took %.1f s", what, age,
                      time.monotonic() - started)

    def _view(self, buttons: list[Button]):
        import discord

        view = discord.ui.View(timeout=None)
        for b in buttons[:MAX_BUTTONS]:
            if len(b.data) <= MAX_CUSTOM_ID:  # the typed fallback still works without it
                view.add_item(discord.ui.Button(label=b.label[:MAX_LABEL], custom_id=b.data))
        return view

    async def _dm(self, user_id: str):
        user = self.client.get_user(int(user_id)) or await self.client.fetch_user(int(user_id))
        return user.dm_channel or await user.create_dm()

    def _connected(self) -> bool:
        return self.client is not None and self.client.is_ready()

    async def send(self, user_id: str, reply: Reply) -> SendResult:
        if not self._connected():
            return SendResult(False, reason="not-connected")
        chunks = split_text(reply.text, MAX_TEXT)
        last = None
        try:
            dm = await self._dm(user_id)
            for i, chunk in enumerate(chunks):
                view = self._view(reply.buttons) if reply.buttons and i == len(chunks) - 1 else None
                last = await dm.send(chunk, view=view)
                if view is not None:
                    view.stop()  # taps are read from the raw interaction: nothing to keep in memory
        except Exception:
            if last is None:
                log.exception("discord send failed")
                return SendResult(False, reason="error")
            # earlier parts are delivered; reporting failure would make the caller re-send them all
            log.exception("discord send partially failed")
        return SendResult(True, str(last.id) if last is not None else None)

    async def send_image(self, user_id: str, path: Path, caption: str = "") -> SendResult:
        if not self._connected():
            return SendResult(False, reason="not-connected")
        import discord

        try:
            dm = await self._dm(user_id)
            sent = await dm.send(caption or None, file=discord.File(str(path), filename=path.name))
        except Exception:
            log.exception("discord send_image failed")
            return SendResult(False, reason="error")
        return SendResult(True, str(sent.id))

    async def send_file(self, user_id: str, data: bytes, filename: str) -> SendResult:
        if not self._connected():
            return SendResult(False, reason="not-connected")
        if len(data) > MAX_UPLOAD:
            return SendResult(False, reason="too-big")
        import discord

        try:
            dm = await self._dm(user_id)
            sent = await dm.send(file=discord.File(io.BytesIO(data), filename=filename))
        except Exception:
            log.exception("discord send_file failed")
            return SendResult(False, reason="error")
        return SendResult(True, str(sent.id))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def handle_message(self, m, on_message: OnMessage) -> None:
        """Returns at once: the message is read (its files downloaded) in the background, and handed to App only after
        the same user's previous message was, so a text sent after a slow file never overtakes it (as on Telegram)."""
        author = getattr(m, "author", None)
        user_id = str(author.id) if author is not None else ""
        before = self._last.get(user_id)

        async def read_then_hand_over() -> None:
            try:
                incoming = await asyncio.wait_for(self.to_incoming(m), READ_TIMEOUT_SECONDS)
            except TimeoutError:
                log.warning("discord: reading a message from %s took over %s s, given up", user_tag(user_id),
                            READ_TIMEOUT_SECONDS)
                incoming = None
                await tell_dropped(self, user_id, "read timed out", t("channels.download_failed"))
            except Exception:
                log.exception("discord: could not read incoming message")
                incoming = None
            if before is not None:
                await asyncio.wait({before})
            if incoming is not None:
                self._spawn(on_message(incoming))

        self._in_order(user_id, asyncio.create_task(read_then_hand_over()))

    def _in_order(self, user_id: str, task: asyncio.Task) -> None:
        """task hands one message (or tap, or command) to App; the same user's next one waits for that."""
        self._tasks.add(task)
        self._last[user_id] = task

        def done(done_task: asyncio.Task) -> None:
            self._tasks.discard(done_task)
            if self._last.get(user_id) is done_task:
                del self._last[user_id]

        task.add_done_callback(done)

    async def handle_callback(self, interaction, on_message: OnMessage) -> None:
        await self._handle_interaction(interaction, self.on_callback, on_message)

    async def handle_command(self, interaction, on_message: OnMessage) -> None:
        await self._handle_interaction(interaction, self.on_command, on_message)

    async def _handle_interaction(self, interaction, read_fn, on_message: OnMessage) -> None:
        """Answered right here in the gateway handler, before anything waits (Discord wants the answer within 3 s of
        the tap), but handed to App only after the same user's message sent before it, as messages are. Its place
        in that order is taken at once, so a message sent while the answer is on its way waits for it."""
        user = getattr(interaction, "user", None)
        user_id = str(user.id) if user is not None else ""
        before = self._last.get(user_id)
        answered: asyncio.Future = asyncio.get_running_loop().create_future()

        async def hand_over() -> None:
            incoming = await answered
            if before is not None:
                await asyncio.wait({before})
            if incoming is not None:
                self._spawn(on_message(incoming))

        self._in_order(user_id, asyncio.create_task(hand_over()))
        incoming = None
        try:
            incoming = await read_fn(interaction)
        except Exception:
            log.exception("discord: could not read an interaction")
        finally:
            if not answered.done():  # hand_over cancelled (shutdown) cancels it too
                answered.set_result(incoming)

    async def drain(self) -> None:
        while running := [task for task in self._tasks if not task.done()]:
            await asyncio.gather(*running, return_exceptions=True)

    async def sync_commands(self) -> None:
        """Puts our commands in Discord's "/" menu when what Discord has differs (one GET a start; the PUT only on a
        change, Discord limits creates per day). Never fatal: typed /commands work without it."""
        from discord.http import Route

        try:
            app_id = self.client.application_id
            route = Route("GET", "/applications/{application_id}/commands", application_id=app_id)
            current = await self.client.http.request(route, params={"with_localizations": "true"})
            wanted = command_payload()
            if _shape(current) == _shape(wanted):
                log.debug("discord: the / commands are up to date")
                return
            await self.client.http.bulk_upsert_global_commands(app_id, wanted)
            log.info("discord: registered %d commands for the / menu", len(wanted))
        except Exception as e:
            self._commands_synced = False  # tried again on the next on_ready
            log.warning("discord: could not register the / commands (%s: %s); typed commands still work",
                        type(e).__name__, e)

    async def start(self, on_message: OnMessage) -> None:
        import aiohttp
        import discord

        logging.getLogger("discord").setLevel(logging.WARNING)  # gateway chatter at INFO
        # only DMs: their words come without the privileged Message Content intent; no server traffic at all
        # guilds: not privileged, little traffic; without it discord.py warns on every start
        connector = aiohttp.TCPConnector(limit=0, keepalive_timeout=KEEP_ALIVE_SECONDS)  # discord.py's, kept longer
        try:
            client = discord.Client(intents=discord.Intents(dm_messages=True, guilds=True), connector=connector)
        except BaseException:
            await connector.close()
            raise
        self.client = client
        handler = on_message  # the event below is named on_message too (discord.py finds events by name)

        @client.event
        async def on_message(m):
            await self.handle_message(m, handler)

        @client.event
        async def on_interaction(interaction):
            if interaction.type == discord.InteractionType.component:
                await self.handle_callback(interaction, handler)
            elif interaction.type == discord.InteractionType.application_command:
                await self.handle_command(interaction, handler)

        @client.event
        async def on_ready():
            log.info("discord: connected")
            if not self._commands_synced:  # on_ready comes again after a reconnect; checked again only after a failure
                self._commands_synced = True
                self._spawn(self.sync_commands())

        try:
            async with client:
                await client.start(self.token)
        except discord.LoginFailure:
            raise RuntimeError(t("discord.token_rejected")) from None
        finally:
            await self.drain()
