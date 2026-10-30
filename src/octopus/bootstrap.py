"""Wire config → database → library/index/LLM → App."""
from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from logging.handlers import RotatingFileHandler

from octopus.app import App
from octopus.channels.base import Channel
from octopus.config import Config, ensure_dirs
from octopus.db import connect
from octopus.embed import Embedder, OllamaEmbedder
from octopus.fetchers import default_registry
from octopus.i18n import t
from octopus.index import Index
from octopus.library import Library
from octopus.media import MediaTools
from octopus.llm.router import LLMRouter, build_providers
from octopus.secrets import store_name
from octopus.settings import Settings

log = logging.getLogger(__name__)
_HANDLER_TAG = "_octopus_log_handler"
SHUTDOWN_TIMEOUT = 15  # seconds for the whole of `octopus run`'s shutdown


def setup_logging(cfg: Config) -> None:
    ensure_dirs(cfg)  # the log sits under data_dir: create it private (0700), never 0755 by accident
    root = logging.getLogger()
    # Idempotent: drop any handler a previous call installed so repeated calls don't stack or leak.
    for old in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(old)
        old.close()
    handler = RotatingFileHandler(cfg.logs_dir / "octopus.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(handler, _HANDLER_TAG, True)
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "jieba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_app(cfg: Config, channels: dict[str, Channel], embedder: Embedder | None = None,
              media: MediaTools | None = None, claude_token: str | None = None) -> App:
    ensure_dirs(cfg)
    embedder = embedder or OllamaEmbedder(cfg.embed.base_url, cfg.embed.model, cfg.embed.dim)
    conn = connect(cfg.db_path, embed_dim=embedder.dim)
    library = Library(cfg.library_dir, cfg.trash_dir, cfg.attachments_dir)
    index = Index(conn, embedder)
    llm = LLMRouter(build_providers(cfg, claude_token), {"summarize": cfg.summarize, "answer": cfg.answer}, Settings(conn))
    if media is None:  # models load lazily on first use, never at construction
        from octopus.media.asr import make_transcriber
        from octopus.media.ocr import make_ocr
        from octopus.media.pdf import pdf_text

        ocr = make_ocr(cfg.media.ocr_backend)  # macOS: mlx + ocrmac; Linux: faster-whisper + rapidocr (P22)
        media = MediaTools(transcribe=make_transcriber(cfg.media), ocr=ocr, pdf=partial(pdf_text, ocr=ocr),
                           attachments_dir=cfg.attachments_dir, downloads_dir=cfg.downloads_dir,
                           incoming_dir=cfg.incoming_dir)
    registry = default_registry(cfg.downloads_dir, cfg.media)
    return App(cfg, conn=conn, library=library, index=index, llm=llm, registry=registry, channels=channels,
               media=media)


def build_channels(cfg: Config, secrets, notify) -> tuple[dict[str, Channel], list[str]]:
    """Channels for every enabled entry that has its secrets, plus problems (for the user) for the ones that don't."""
    channels: dict[str, Channel] = {}
    problems: list[str] = []
    store = store_name(cfg.secrets_backend)  # where a missing secret belongs on this system
    conf = cfg.channels

    def allow_list(name: str):
        # bind the name now: one closure per channel, each reading its own allowed_users
        return lambda uid, _name=name: uid in cfg.channels[_name].allowed_users

    if conf.get("telegram") and conf["telegram"].enabled:
        token = secrets.get("telegram_token")
        if token:
            from octopus.channels.telegram import TelegramChannel

            channels["telegram"] = TelegramChannel(token, cfg.incoming_dir, allowed=allow_list("telegram"))
        else:
            problems.append(t("bootstrap.no_telegram_token", store=store))
    if conf.get("feishu") and conf["feishu"].enabled:
        app_id, secret = secrets.get("feishu_app_id"), secrets.get("feishu_app_secret")
        if app_id and secret:
            from octopus.channels.feishu import FeishuChannel

            channels["feishu"] = FeishuChannel(app_id, secret, cfg.incoming_dir, allowed=allow_list("feishu"))
        else:
            problems.append(t("bootstrap.no_feishu_secrets", store=store))
    if conf.get("clawbot") and conf["clawbot"].enabled:
        from octopus.channels.clawbot import ClawBotChannel

        channels["clawbot"] = ClawBotChannel(secrets, cfg.data_dir / "clawbot-state.json", cfg.incoming_dir,
                                             cfg.data_dir / "clawbot-qr.png", notify=notify,
                                             allowed=allow_list("clawbot"))
    if conf.get("discord") and conf["discord"].enabled:
        token = secrets.get("discord_bot_token")
        if token:
            from octopus.channels.discord import DiscordChannel

            channels["discord"] = DiscordChannel(token, cfg.incoming_dir, allowed=allow_list("discord"))
        else:
            problems.append(t("bootstrap.no_discord_token", store=store))
    if cfg.email.enabled:
        from octopus.mail import trusted_servers

        password = secrets.get("email_password")
        if not trusted_servers(cfg.email):  # no way to tell the provider's own check from a forged one: never start
            problems.append(t("mail.unknown_server", host=cfg.email.imap_host))
        elif password:
            from octopus.channels.email import EmailChannel

            channels["email"] = EmailChannel(cfg.email, password)
        else:
            problems.append(t("bootstrap.no_email_password", store=store))
    if cfg.extension.enabled:
        token = secrets.get("extension_token")
        if token:
            from octopus.channels.extension import ExtensionChannel

            channels["extension"] = ExtensionChannel(token, cfg.extension.port, cfg.extension.bind)
        else:
            problems.append(t("bootstrap.no_extension_token", store=store))
    return channels, problems


async def _wait_or_stop(stop: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        pass


async def run_service(app, channels: dict[str, Channel], stop: asyncio.Event, restart_delay: float = 5.0,
                      max_delay: float = 300.0, shutdown_timeout: float = SHUTDOWN_TIMEOUT) -> bool:
    """Run the job worker and every channel until `stop` is set; a crashed channel restarts with backoff.

    Returns False when the job worker ended on its own (the service cannot save anything: stop, so launchd
    restarts us), True after a requested stop. Shutdown: the worker stops claiming jobs and finishes the ones
    it holds while the channels can still deliver their replies; then the channels go. All of it is bounded by
    shutdown_timeout; jobs interrupted by the limit are re-queued by recover() on the next start."""
    loop = asyncio.get_running_loop()

    async def supervise(name: str, ch: Channel) -> None:
        delay = restart_delay
        while not stop.is_set():
            started = time.monotonic()
            try:
                await ch.start(app.handle)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("channel %s crashed; restarting in %.0fs", name, delay)
            else:
                if stop.is_set():
                    break
                log.warning("channel %s stopped (start() returned); restarting in %.0fs", name, delay)
            if time.monotonic() - started > 60:
                delay = restart_delay  # it ran fine for a while: reset the backoff
            await _wait_or_stop(stop, delay)
            delay = min(delay * 2, max_delay)

    worker = asyncio.create_task(app.run_worker(stop))
    sups = [asyncio.create_task(supervise(n, c)) for n, c in channels.items()]
    stop_task = asyncio.create_task(stop.wait())
    ok = True
    try:
        await asyncio.wait({stop_task, worker}, return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():  # the worker ended although nobody asked it to
            ok = False
            exc = None if worker.cancelled() else worker.exception()
            log.error("job worker stopped unexpectedly; shutting down", exc_info=exc)
    finally:
        stop_task.cancel()
        stop.set()  # the worker stops claiming new jobs
        app.wake.set()
        deadline = loop.time() + shutdown_timeout
        # asyncio.wait does not forward a cancellation into the worker: its in-flight jobs finish and reply
        await asyncio.wait({worker}, timeout=shutdown_timeout)
        if not worker.done():
            log.warning("job worker did not finish within %.0fs; cancelling it (interrupted jobs run again "
                        "next start)", shutdown_timeout)
            worker.cancel()
        for task in sups:
            task.cancel()
        tasks = {worker, *sups}
        # at least one turn of the loop so the first cancellation lands before a second one
        _, pending = await asyncio.wait(tasks, timeout=max(deadline - loop.time(), 0.1))
        if pending:
            log.warning("%d task(s) still running after the %.0fs shutdown limit; cancelling them",
                        len(pending), shutdown_timeout)
            for task in pending:
                task.cancel()
            _, pending = await asyncio.wait(pending, timeout=1.0)
            if pending:
                log.error("%d task(s) ignored cancellation; exiting anyway", len(pending))
        if ok and worker.done() and not worker.cancelled() and worker.exception() is not None:
            log.error("job worker failed", exc_info=worker.exception())
    return ok
