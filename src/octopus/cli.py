"""`octopus` command line: init / chat / run / clawbot-login / reindex / backup / secret / extension / models."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import secrets as random_tokens
import signal
import subprocess
import sys
import time
import tomllib
from datetime import date
from pathlib import Path

from octopus import bootstrap, host, i18n, service, systemd
from octopus.backup import backup_db, prune
from octopus.channels.cli import CLIChannel
from octopus.embed import EmbedError
from octopus.config import DEFAULT_DATA_DIR, ConfigError, api_key_provider, ensure_dirs, load_config, write_default_config
from octopus.i18n import t
from octopus.instance import InstanceLock
from octopus.commands import task_name
from octopus.llm.api import ATTEMPTS, MAX_RETRY_AFTER
from octopus.llm.router import build_providers
from octopus.media import missing_backend
from octopus.secrets import (FILE_NAME as SECRETS_FILE, MANAGED, SECRET_NAMES, Secrets, SecretsFileError, api_key_names,
                             env_name, find_api_key, from_env, keep_name, keyring_label, secret_label)

log = logging.getLogger(__name__)
MODELS_TEST_TIMEOUT = (ATTEMPTS - 1) * MAX_RETRY_AFTER + 30  # per model: a busy 529 retried in full still shows as 529
__getattr__ = i18n.aliases({  # the old constant names, in the configured language
    "ALREADY_RUNNING": "cli.already_running", "NO_SERVICE": "cli.no_service",
    "SERVICE_SECRETS_MAC": "cli.service_secrets_mac", "SERVICE_SECRETS_LINUX": "cli.service_secrets_linux",
    "ENV_WINS": "cli.env_wins", "ENV_KEY_ON_MAC": "cli.env_key_on_mac", "ENV_KEY_ON_LINUX": "cli.env_key_on_linux",
    "TOKEN_COPIED": "cli.token_copied", "COPY_FAILED": "cli.copy_failed",
    "COPY_UNAVAILABLE": "cli.copy_unavailable",
})


def _service():
    """The service backend of this system: launchd on macOS, systemd on Linux, None elsewhere."""
    return {host.MAC: service, host.LINUX: systemd}.get(host.system())


def _secrets(cfg) -> Secrets:
    """Environment, then the keyring ([secrets] backend), then <data_dir>/secrets.toml (P22)."""
    return Secrets(file=cfg.data_dir / SECRETS_FILE, mode=cfg.secrets_backend)


def _env_note(where: str | None) -> str:
    """A key found only in this terminal's environment: the background service will not see it."""
    if not from_env(where):
        return ""
    if host.system() == host.MAC:
        return t("cli.env_key_on_mac")
    return t("cli.env_key_on_linux") if host.system() == host.LINUX and not host.in_container() else ""


def _secrets_for(path: Path):
    """`secret` / `extension token`: (Secrets, cfg, config problem). On a Mac a config.toml that does not load
    falls back to the Keychain alone (it needs no data_dir; as before P22); elsewhere the secrets file lives in its
    data_dir, so (None, None, problem) and exit 2."""
    try:
        cfg = load_config(path)
    except ConfigError as e:
        problem = str(e)
    except (ValueError, TypeError, OSError) as e:
        problem = t("cli.config_error", error=e)
    else:
        return _secrets(cfg), cfg, None
    if host.system() == host.MAC:
        return Secrets(), None, problem
    print(problem)
    return None, None, problem


async def run_chat(app, ch: CLIChannel) -> None:
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))

    async def drain_worker() -> None:
        stop.set()
        app.wake.set()
        # asyncio.wait does not forward our cancellation into the worker (its in-flight jobs must finish).
        await asyncio.wait({worker})
        try:
            worker.result()
        except Exception:
            log.exception("job worker failed")

    try:
        ch.output(t("cli.chat_started"))
        await ch.start(app.handle)
        if app.jobs.due_count():
            ch.output(t("cli.chat_waiting"))
            await app.process_due_jobs()
        await drain_worker()  # jobs the worker already holds finish before we count what is left
        pending = sum(app.jobs.counts().get(s, 0) for s in ("queued", "running"))
        if pending:
            ch.output(t("cli.chat_unfinished", n=pending))
    finally:
        try:
            await drain_worker()
        finally:
            app.close()


async def _chat(cfg) -> int:
    lock = InstanceLock(cfg.data_dir / "octopus.lock")
    if not lock.acquire():
        print(t("cli.already_running"))
        return 1
    try:
        ch = CLIChannel(incoming_dir=cfg.incoming_dir)
        app = bootstrap.build_app(cfg, {ch.name: ch}, claude_token=_secrets(cfg).get("claude_oauth_token"))
        await run_chat(app, ch)
        return 0
    finally:
        lock.release()


def _problem(text: str) -> None:
    """A startup problem: on the terminal and in octopus.log (under launchd nobody sees the terminal)."""
    print(text)
    log.error(text)


async def _run(cfg) -> int:
    lock = InstanceLock(cfg.data_dir / "octopus.lock")
    if not lock.acquire():
        _problem(t("cli.already_running"))
        return 1  # non-zero so a launchd KeepAlive job sees the failure
    try:
        return await _serve(cfg)
    except Exception:
        log.exception("octopus run failed")
        raise
    finally:
        lock.release()


async def _serve(cfg) -> int:
    secrets = _secrets(cfg)
    holder: dict = {}

    async def notify(text, image):
        # the notice is about ClawBot (QR code): it cannot deliver it to itself
        return await holder["app"].notify_owner(text, image, exclude_channel="clawbot")

    channels, problems = bootstrap.build_channels(cfg, secrets, notify)
    for p in problems:
        _problem(p)
    if not channels:
        _problem(t("cli.no_channels"))
        return 1
    app = bootstrap.build_app(cfg, channels, claude_token=secrets.get("claude_oauth_token"))
    holder["app"] = app
    app.commands.clawbot = channels.get("clawbot")  # /clawbot asks the running channel for a fresh QR code
    if (extension := channels.get("extension")) is not None:  # the endpoint saves through the App's jobs
        from octopus.extension import Intake

        extension.intake = Intake(app)
    if (mailbox := channels.get("email")) is not None:  # mails are saved through the App's jobs too
        from octopus.mail import MailIntake

        mailbox.intake = MailIntake(app, cfg.email, mailbox.send_mail)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, stop.set)
    print(t("cli.running", channels=", ".join(channels)))
    try:
        ok = await bootstrap.run_service(app, channels, stop)
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        app.close()
    return 0 if ok else 1  # the job worker died: launchd KeepAlive restarts us


async def _clawbot_login(cfg) -> int:
    ensure_dirs(cfg)  # the QR image and the state file go under the (0700) data dir
    lock = InstanceLock(cfg.data_dir / "octopus.lock")
    if not lock.acquire():
        print(t("cli.clawbot_running"))
        return 1
    try:
        import keyring.errors

        from octopus.channels import clawbot
        from octopus.channels.ilink import ILinkError

        try:
            res = await clawbot.login_interactive(_secrets(cfg), cfg.data_dir / "clawbot-state.json",
                                                  cfg.data_dir / "clawbot-qr.png")
        except ILinkError as e:
            print(t("cli.clawbot_failed", error=e))
            return 1
        except keyring.errors.KeyringError as e:
            print(t("cli.clawbot_keyring", store=keyring_label(), error=type(e).__name__))
            return 1
        except SecretsFileError as e:
            print(t("cli.clawbot_not_saved", error=e))
            return 1
        print(t("cli.clawbot_account", user=res.user_id or t("common.unknown")))
        print(t("cli.clawbot_enable"))
        return 0
    finally:
        lock.release()


NOT_MARKED = -1  # _reindex: the library folder lacks its marker (moved away? not set up since the upgrade?)
MARKER_GONE = -2  # _reindex: the notes are there but the marker was deleted after the upgrade


async def _reindex(cfg) -> int | None:
    """The number of entries indexed, None when the service is running (it owns the index), NOT_MARKED when the
    folder is not known to be this library's (an empty folder made again would otherwise empty the index),
    MARKER_GONE when its notes are there but its marker was deleted."""
    lock = InstanceLock(cfg.data_dir / "octopus.lock")
    if not lock.acquire():
        return None
    try:
        embedder = bootstrap.OllamaEmbedder(cfg.embed.base_url, cfg.embed.model, cfg.embed.dim)
        await embedder.embed(["ping"])  # Index.upsert swallows embed errors; fail fast instead of indexing without vectors
        app = bootstrap.build_app(cfg, {}, embedder=embedder)
        try:
            from octopus.library import read_marker
            from octopus.sync import library_ok, marker_evidence

            root, entries = app.library.root, app.index.count()
            mark = read_marker(root)
            evidence = mark is None and entries > 0 and marker_evidence(root, app.index.paths())
            if not library_ok(root, app.conn, entries, mark, evidence):
                return MARKER_GONE if evidence else NOT_MARKED
            return await app.index.rebuild(app.library)
        finally:
            app.close()
    finally:
        lock.release()


def _backup(cfg) -> tuple[Path, bool]:
    """(backup file, newly made). SQLite's online backup is safe beside the service, so no instance lock."""
    app = bootstrap.build_app(cfg, {})  # ensure_dirs + open the database
    try:
        made = backup_db(app.conn, cfg.backups_dir, date.today())
        prune(cfg.backups_dir)
        return (made or cfg.backups_dir / f"octopus-{date.today():%Y%m%d}.db"), made is not None
    finally:
        app.close()


def _library_move(cfg, config_path: Path, target: Path, adopt: bool = False) -> int:
    from octopus.move import MoveError, adopt_library, move_library

    lock = InstanceLock(cfg.data_dir / "octopus.lock")
    if not lock.acquire():
        print(t("cli.move_running"))
        return 1
    try:
        if adopt:
            n = adopt_library(cfg, config_path, target)
            print(t("cli.adopted", path=Path(target).expanduser().resolve(), n=n))
            return 0
        res = move_library(cfg, config_path, target)
    except MoveError as e:
        print(e)
        return 1
    finally:
        lock.release()
    print(t("cli.moved", target=res.target, entries=res.entries, files=res.files))
    if res.backup:
        print(t("cli.move_backup", backup=res.backup))
    print(t("cli.move_obsidian"))
    return 0


async def _library_sync(cfg, apply: bool) -> int:
    """The deletions the service holds back (many notes gone at once): list them, or with apply drop them from the
    index (files and attachments are not touched)."""
    from octopus.sync import LibrarySync

    lock = InstanceLock(cfg.data_dir / "octopus.lock")
    if not lock.acquire():
        print(t("cli.sync_running"))
        return 1
    try:
        app = bootstrap.build_app(cfg, {})
        try:
            sync = LibrarySync(app.pipeline)
            gone = await sync.held_deletes()
            if gone is None:
                print(t("cli.sync_no_library"))
                return 1
            if not gone:
                print(t("cli.sync_none"))
                return 0
            titles = dict(app.index.titles())
            if not apply:
                lines = "\n".join(t("cli.sync_entry", id=i, title=titles.get(i, "")) for i in gone)
                print(t("cli.sync_list", n=len(gone), lines=lines))
                return 0
            n = sum([await app.pipeline.forget(i) for i in gone])
            print(t("cli.sync_applied", n=n))
            return 0
        finally:
            app.close()
    finally:
        lock.release()


def _mark_library(cfg) -> int:
    """init: mark the library folder as this database's (P18: scans and reindex trust only a marked folder)."""
    from octopus.db import connect
    from octopus.sync import claim_library

    conn = connect(cfg.db_path, embed_dim=cfg.embed.dim)
    try:
        if not claim_library(cfg.library_dir, conn):
            print(t("cli.marker_foreign", path=cfg.library_dir))
            return 1
    finally:
        conn.close()
    return 0


def _load(path: Path):
    """The config, or None after telling the user what is wrong with it (exit code 2)."""
    try:
        cfg = load_config(path)
    except ConfigError as e:
        print(e)
    except (ValueError, TypeError, OSError) as e:  # e.g. max_concurrent_jobs = "lots", a directory as the path
        print(t("cli.config_error", error=e))
    else:
        if problem := missing_backend(cfg.media):  # P22: e.g. asr_backend = "mlx" on Linux
            print(problem)
            return None
        return cfg
    return None


def _api_providers(cfg) -> list[str]:
    """The API-key providers the config uses: "anthropic" and [providers] names, in order of first use."""
    out: list[str] = []
    for task in (cfg.summarize, cfg.answer):
        for spec in (task.primary, task.fallback):
            if (name := api_key_provider(spec)) and name not in out:
                out.append(name)
    return out


def _key_lines(cfg) -> list[str]:
    """One line per API provider: whether its key is set and where, never the key itself."""
    providers = _api_providers(cfg)
    if not providers:
        return []
    secrets = _secrets(cfg)
    lines = [t("cli.api_keys")]
    for name in providers:
        _, where = find_api_key(secrets, name)
        secret, env = api_key_names(name)
        if where:
            lines.append(t("cli.key_set", name=name, where=where, note=_env_note(where)))
        else:
            local = t("cli.key_local") if name != "anthropic" else ""
            lines.append(t("cli.key_unset", name=name, secret=secret, env=env, local=local))
    return lines


def _models(cfg) -> str:
    lines = [t("common.label", label=task_name(task),
               value=t("common.model_fallback", model=m.primary, fallback=m.fallback)
               if m.fallback and m.fallback != m.primary else m.primary)
             for task, m in (("summarize", cfg.summarize), ("answer", cfg.answer))]
    return "\n".join(lines + _key_lines(cfg))


async def _models_test(cfg) -> int:
    """A tiny prompt to every model the config names (and the local one): 0 when all of them answer."""
    providers = build_providers(cfg, claude_token=_secrets(cfg).get("claude_oauth_token"))
    failed = 0
    for name, provider in providers.items():
        start = time.monotonic()
        try:
            await asyncio.wait_for(provider.complete(t("prompt.test.prompt"), t("prompt.test.system")), MODELS_TEST_TIMEOUT)
        except TimeoutError:
            failed += 1
            print(t("cli.test_timeout", name=name, seconds=MODELS_TEST_TIMEOUT))
        except Exception as e:  # any provider error is a result to show
            failed += 1
            print(t("cli.test_failed", name=name, error=e))
        else:
            print(t("cli.test_ok", name=name, seconds=f"{time.monotonic() - start:.1f}"))
    return 1 if failed else 0


def _secret_set(secrets, name: str) -> int:
    try:
        value = getpass.getpass(t("cli.secret_prompt", label=secret_label(name))).strip()
    except (KeyboardInterrupt, EOFError):
        print(t("cli.cancelled"))
        return 1
    if not value:
        print(t("cli.nothing_entered"))
        return 1
    import keyring.errors

    try:
        where = secrets.set(name, value) or keyring_label()
    except SecretsFileError as e:
        print(t("cli.save_failed", error=e))
        return 1
    except (keyring.errors.KeyringError, OSError) as e:
        print(t("cli.save_keyring_failed", store=keyring_label(), error=str(e) or type(e).__name__))
        return 1
    print(t("cli.saved", where=where, name=name))
    if name not in MANAGED and os.environ.get(env := env_name(name), "").strip():
        print(t("cli.env_wins", env=env))
    return 0


def _extension_token(path: Path, copy: bool = False) -> int:
    """Prints the Chrome extension's password, made (32 random bytes) and kept in the Keychain (or the secrets
    file) on first use. copy: also onto the clipboard (pbcopy), for the Shortcuts' import question (P23)."""
    import keyring.errors

    secrets, cfg, problem = _secrets_for(path)
    if secrets is None:
        return 2
    token = secrets.get("extension_token")
    if not token:
        token = random_tokens.token_urlsafe(32)
        try:
            secrets.set("extension_token", token)
        except SecretsFileError as e:
            print(t("cli.token_not_saved", error=e))
            return 1
        except (keyring.errors.KeyringError, OSError) as e:
            print(t("cli.token_keyring", store=keyring_label(), error=str(e) or type(e).__name__))
            return 1
    print(t("cli.token", token=token))
    env = env_name("extension_token")
    if token == os.environ.get(env, "").strip() and (note := _env_note(t("secrets.where_env", env=env))):
        print(t("cli.token_from_env", env=env, note=note))  # the service reads the stored one (int2 M2)
    if copy:
        try:
            subprocess.run(["pbcopy"], input=token.encode(), check=True, timeout=5)
            print(t("cli.token_copied"))
        except FileNotFoundError:
            print(t("cli.copy_unavailable"))  # no pbcopy: not a Mac
        except (OSError, subprocess.SubprocessError):
            print(t("cli.copy_failed"))
    if cfg is None:
        print(problem)
    elif not cfg.extension.enabled:
        print(t("cli.extension_enable"))
    return 0


def _config_language(argv: list[str]) -> str:
    """--help and argparse's errors come before the config is loaded: they are said in its language when it names
    one (no `language` line or a broken config: Chinese; no config file: the system's language). Returns its [secrets] backend (for --help). --config is
    found as written: the parser takes no abbreviated options (allow_abbrev=False)."""
    path = DEFAULT_DATA_DIR / "config.toml"
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            path = Path(argv[i + 1])
        elif arg.startswith("--config="):
            path = Path(arg.partition("=")[2])
    try:
        missing = not path.expanduser().exists()
    except OSError:  # e.g. another user's 0700 folder: taken as there (and unreadable), as before P28
        missing = False
    if missing:
        i18n.set_language(i18n.detected_language())
        return "auto"
    try:
        raw = tomllib.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raw = {}
    language = raw.get("language")
    i18n.set_language(language if language in i18n.LANGUAGES else i18n.DEFAULT_LANGUAGE)
    backend = raw.get("secrets", {}).get("backend", "auto") if isinstance(raw.get("secrets"), dict) else "auto"
    return backend if isinstance(backend, str) else "auto"


def _write_new_config(path: Path, language: str | None = None) -> bool:
    """A default config at path in language, or else the system's (False: one is there, left as it is); says so,
    from then on in that language."""
    if path.exists():
        return False
    chosen = language or i18n.detected_language()
    if not write_default_config(path, chosen):  # another process wrote one first
        return False
    i18n.set_language(chosen)
    print(t("cli.config_written", path=path))
    if language is None:
        print(t("cli.language_from_system", path=path))
    return True


def main(argv: list[str] | None = None) -> int:
    backend = _config_language(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="octopus", allow_abbrev=False)
    parser.add_argument("--config", type=Path, default=DEFAULT_DATA_DIR / "config.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init", help=t("cli.help.init"))
    init.add_argument("--language", choices=i18n.LANGUAGES, help=t("cli.help.language"))
    sub.add_parser("chat", help=t("cli.help.chat"))
    sub.add_parser("reindex", help=t("cli.help.reindex"))
    sub.add_parser("backup", help=t("cli.help.backup"))
    sub.add_parser("run", help=t("cli.help.run"))
    sub.add_parser("clawbot-login", help=t("cli.help.clawbot_login"))
    lib = sub.add_parser("library", help=t("cli.help.library"))
    lib_sub = lib.add_subparsers(dest="library_cmd", required=True)
    lib_move = lib_sub.add_parser("move", help=t("cli.help.library_move"))
    lib_move.add_argument("dir", type=Path)
    lib_adopt = lib_sub.add_parser("adopt", help=t("cli.help.library_adopt"))
    lib_adopt.add_argument("dir", type=Path)
    lib_sync = lib_sub.add_parser("sync", help=t("cli.help.library_sync"))
    lib_sync.add_argument("--apply-deletes", action="store_true")
    sec = sub.add_parser("secret", help=t("cli.help.secret"))
    sec_sub = sec.add_subparsers(dest="secret_cmd", required=True)
    s_set = sec_sub.add_parser("set", help=t("cli.help.secret_set"))
    s_set.add_argument("name")
    sec_sub.add_parser("list", help=t("cli.help.secret_list"))
    ext = sub.add_parser("extension", help=t("cli.help.extension"))
    ext_sub = ext.add_subparsers(dest="extension_cmd", required=True)
    ext_tok = ext_sub.add_parser("token", help=t("cli.help.extension_token", store=keep_name(backend)))
    ext_tok.add_argument("--copy", action="store_true", help=t("cli.help.copy"))
    mod = sub.add_parser("models", help=t("cli.help.models"))
    mod.add_argument("action", nargs="?", choices=["test"])
    svc = sub.add_parser("service", help=t("cli.help.service"))
    svc_sub = svc.add_subparsers(dest="service_cmd", required=True)
    svc_sub.add_parser("install", help=t("cli.help.service_install"))
    svc_sub.add_parser("restart", help=t("cli.help.service_restart"))
    svc_sub.add_parser("stop", help=t("cli.help.service_stop"))
    svc_sub.add_parser("start", help=t("cli.help.service_start"))
    svc_sub.add_parser("uninstall", help=t("cli.help.service_uninstall"))
    svc_sub.add_parser("status", help=t("cli.help.service_status"))
    args = parser.parse_args(argv)

    if args.cmd == "init":
        if not _write_new_config(args.config, args.language):  # an existing config keeps its own language
            print(t("cli.config_exists", path=args.config))
        if (cfg := _load(args.config)) is None:
            return 2
        ensure_dirs(cfg)  # the data dir is private (0700) from the first run
        return _mark_library(cfg)
    if args.cmd == "secret":
        secrets, _, _ = _secrets_for(args.config)
        if secrets is None:
            return 2
        if args.secret_cmd == "list":
            marks = {True: t("cli.secret_is_set"), False: t("cli.secret_not_set"),
                     None: t("cli.secret_unreadable")}  # None: locked or denied Keychain
            for name, (present, where) in secrets.status().items():  # where it comes from, never the value
                source = t("cli.secret_source", where=where, note=_env_note(where)) if where else ""
                print(t("cli.secret_line", name=name, mark=marks[present], source=source, label=secret_label(name)))
            return 0
        if secret_label(args.name) is None:
            print(t("cli.unknown_secret", name=args.name, names=", ".join(SECRET_NAMES)))
            return 2
        return _secret_set(secrets, args.name)
    if args.cmd == "extension":
        return _extension_token(args.config, copy=args.copy)
    svc = _service()
    if args.cmd == "service" and svc is None:
        print(t("cli.no_service"))
        return 1
    if args.cmd == "service" and args.service_cmd in ("stop", "start"):
        ok, message = (svc.stop if args.service_cmd == "stop" else svc.start)()
        print(message)
        return 0 if ok else 1
    if args.cmd == "service" and args.service_cmd not in ("install", "restart"):
        print(svc.uninstall() if args.service_cmd == "uninstall" else svc.status())
        if args.service_cmd == "status":
            try:
                lines = _key_lines(load_config(args.config))
            except ConfigError as e:
                lines = [" ".join(str(e).split())]  # one line: the service may be restarting on this very error
            except (ValueError, TypeError, OSError) as e:
                lines = [" ".join(t("cli.config_error", error=e).split())]
            if lines:
                print("\n".join(lines))
        return 0
    if args.cmd == "service":
        config_path = args.config.expanduser().resolve()  # launchd starts us from another directory
        _write_new_config(config_path)
    if (cfg := _load(args.config)) is None:
        return 2
    if args.cmd == "service":
        ensure_dirs(cfg)  # the service writes logs under the (0700) data dir from its first start
        ok, message = svc.install(sys.executable, config_path, cfg.data_dir.resolve(), claude_bin=cfg.claude_bin,
                                   language=cfg.language)
        print(message)
        if not ok:
            return 1
        print(t("cli.service_secrets_mac" if svc is service else "cli.service_secrets_linux"))
        return 0
    bootstrap.setup_logging(cfg)
    try:
        if args.cmd == "chat":
            return asyncio.run(_chat(cfg))
        elif args.cmd == "run":
            return asyncio.run(_run(cfg))
        elif args.cmd == "clawbot-login":
            return asyncio.run(_clawbot_login(cfg))
        elif args.cmd == "models":
            if args.action == "test":
                return asyncio.run(_models_test(cfg))
            print(_models(cfg))
        elif args.cmd == "library":
            if args.library_cmd == "sync":
                return asyncio.run(_library_sync(cfg, args.apply_deletes))
            return _library_move(cfg, args.config, args.dir, adopt=args.library_cmd == "adopt")
        elif args.cmd == "backup":
            path, made = _backup(cfg)
            print(t("cli.backed_up" if made else "cli.backup_exists", path=path))
        elif args.cmd == "reindex":
            try:
                n = asyncio.run(_reindex(cfg))
            except EmbedError as e:
                print(t("cli.reindex_failed", error=e))
                return 1
            if n is None:
                print(t("cli.reindex_running"))
                return 1
            if n == MARKER_GONE:
                print(t("cli.reindex_marker_gone", path=cfg.library_dir))
                return 1
            if n == NOT_MARKED:
                print(t("cli.reindex_not_marked", path=cfg.library_dir))
                return 1
            print(t("cli.reindexed", n=n))
    except KeyboardInterrupt:
        print(t("cli.exited"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
