"""Configuration loaded from ~/Octopus/config.toml."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from octopus import i18n
from octopus.i18n import t
from octopus.secrets import BACKENDS as SECRETS_BACKENDS, api_key_names

DATA_DIR_ENV = "OCTOPUS_DATA_DIR"  # Docker: /data holds config.toml and everything else, like ~/Octopus on a Mac


def default_data_dir() -> Path:
    return Path(os.environ.get(DATA_DIR_ENV, "").strip() or "~/Octopus").expanduser()


DEFAULT_DATA_DIR = default_data_dir()


__getattr__ = i18n.aliases({  # the old constant names, in the configured language
    "DEFAULT_CONFIG_TOML": "config.template", "BAD_MODEL": "config.bad_model", "NO_PROVIDER": "config.no_provider",
    "BAD_PROVIDER_NAME": "config.bad_provider_name", "BAD_CHOICE": "config.bad_choice", "BAD_BIND": "config.bad_bind",
    "SAME_KEY_ENV": "config.same_key_env",
})



@dataclass
class TaskModels:
    primary: str
    fallback: str | None = None


@dataclass
class LocalLLM:
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3.5:35b-a3b"
    num_ctx: int = 32768


@dataclass
class EmbedConfig:
    base_url: str = "http://127.0.0.1:11434"
    model: str = "bge-m3"
    dim: int = 1024


@dataclass
class ChannelConfig:
    enabled: bool = False
    allowed_users: list[str] = field(default_factory=list)


@dataclass
class ExtensionConfig:
    enabled: bool = False
    port: int = 8765
    bind: str = "127.0.0.1"  # "0.0.0.0" only inside a container, published as 127.0.0.1:<port>:<port>


@dataclass
class EmailConfig:
    """[channels.email] (P23): one dedicated mailbox read over IMAP; save-only. The password is the email_password
    secret."""
    enabled: bool = False
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    username: str = ""
    owner_addresses: list[str] = field(default_factory=list)
    poll_seconds: int = 60
    reply: str = "email"  # "email" | "telegram" | "none"
    # the receiving provider's name in its Authentication-Results header; empty: built in for Gmail (mail.py)
    authserv_id: list[str] = field(default_factory=list)


@dataclass
class MediaConfig:
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    youtube_cookies_from_browser: str = ""  # e.g. "chrome"; empty = never read browser cookies
    max_download_mb: int = 1024
    asr_backend: str = "auto"  # auto: mlx on macOS, faster-whisper elsewhere
    ocr_backend: str = "auto"  # auto: ocrmac on macOS, rapidocr elsewhere
    faster_whisper_model: str = "small"  # CPU int8: fast enough for hour-long podcasts on a small server
    x_full_text_service: str = ""  # "fxtwitter": a long X post's full text from FxTwitter (P29); "": off, never asked


@dataclass
class ProviderConfig:
    """An OpenAI-compatible endpoint named in [providers.<name>], used as "openai:<name>"."""
    base_url: str
    model: str


@dataclass
class Config:
    data_dir: Path
    summarize: TaskModels
    answer: TaskModels
    local: LocalLLM
    embed: EmbedConfig
    channels: dict[str, ChannelConfig]
    claude_bin: str = "claude"
    max_concurrent_jobs: int = 2
    read_batch_parts: int = 5  # /read sends at most this many parts at a time, then a "continue" button (P35)
    media: MediaConfig = field(default_factory=MediaConfig)
    extension: ExtensionConfig = field(default_factory=ExtensionConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    library_folder: Path | None = None  # library_dir in config.toml: e.g. a folder in an Obsidian vault
    secrets_backend: str = "auto"  # [secrets] backend: auto (Keychain on macOS, env + file on Linux) | keyring | file
    language: str = i18n.DEFAULT_LANGUAGE  # what Octopus says: "zh" | "en" (P25); input is understood in both
    summary_language: str = i18n.DEFAULT_LANGUAGE  # summaries: "zh" | "en" | "auto" (the content's); unset: language

    @property
    def library_dir(self) -> Path:
        return self.library_folder or self.data_dir / "library"

    @property
    def library_elsewhere(self) -> bool:
        """The library is not the default data_dir/library (a vault): its attachments live inside it."""
        return self.library_dir.resolve() != (self.data_dir / "library").resolve()

    @property
    def attachments_dir(self) -> Path:
        return self.library_dir / "attachments" if self.library_elsewhere else self.data_dir / "attachments"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "attachments" / "_incoming"  # staging: never in the vault

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "trash"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "octopus.db"

    @property
    def llm_workdir(self) -> Path:
        return self.data_dir / "llm-workdir"


def _task(raw: dict, default_primary: str) -> TaskModels:
    return TaskModels(
        primary=raw.get("primary", default_primary),
        fallback=raw.get("fallback", "local") or None,
    )


class ConfigError(Exception):
    """config.toml is malformed or has settings we do not know; the message is for the user."""


TOP_LEVEL_KEYS = {"data_dir", "library_dir", "claude_bin", "max_concurrent_jobs", "read_batch_parts", "models",
                  "local_llm", "embed", "media", "channels", "providers", "secrets", "language", "summary_language"}
TASK_NAMES = {"summarize", "answer"}
SUMMARY_LANGUAGES = ("auto", "zh", "en")
CHANNEL_NAMES = {"telegram", "feishu", "clawbot", "extension", "discord", "email"}
EXTENSION = "extension"  # the Chrome extension's local endpoint: a port, no allow-list
DISCORD = "discord"  # its allow-list is called owner_user_ids (P23); loaded as allowed_users like the others
EMAIL = "email"  # the save-only mailbox (P23): cfg.email, not in cfg.channels (no chat, no owner notices)
EMAIL_REPLIES = ("email", "telegram", "none")
MODEL_KINDS = ("claude", "anthropic", "openai")  # "<kind>:<model or name>", besides plain "local"
PROVIDER_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
ASR_BACKENDS = ("auto", "mlx", "faster-whisper")
X_FULL_TEXT_SERVICES = ("", "fxtwitter")
OCR_BACKENDS = ("auto", "ocrmac", "rapidocr")


def parse_spec(spec: str) -> tuple[str, str]:
    """("local", "") or (kind, the part after the colon): the one place a model spec is split."""
    if spec == "local":
        return "local", ""
    kind, _, arg = spec.partition(":")
    return kind, arg


def api_key_provider(spec: str | None) -> str | None:
    """Whose API key a spec needs: "anthropic", a [providers] name, or None (claude: / local / no spec)."""
    kind, arg = parse_spec(spec or "")
    return "anthropic" if kind == "anthropic" else arg if kind == "openai" else None


def _parse(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        detail = str(e)
        m = re.search(r"at line (\d+)", detail)
        where = t("config.at_line", path=path, line=m.group(1)) if m else str(path)
        raise ConfigError(t("config.bad_toml", where=where, detail=detail)) from e
    except UnicodeDecodeError as e:
        raise ConfigError(t("config.not_utf8", path=path)) from e


def _table(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(t("config.not_a_table", name=name))
    return value


def _check_keys(section: str, table: dict, allowed: set[str]) -> None:
    for key in table:
        if key not in allowed:
            raise ConfigError(t("config.unknown_key", key=f"{section}.{key}" if section else key))


def _check_model(where: str, spec, providers: dict) -> None:
    # whitespace anywhere ("anthropic: haiku") would load and then fail on every call
    if not isinstance(spec, str) or not spec or any(c.isspace() for c in spec):
        raise ConfigError(t("config.bad_model", where=where, spec=spec))
    kind, arg = parse_spec(spec)
    if kind == "local":
        return
    if kind not in MODEL_KINDS or not arg:
        raise ConfigError(t("config.bad_model", where=where, spec=spec))
    if kind == "openai" and arg not in providers:
        raise ConfigError(t("config.no_provider", where=where, spec=spec, name=arg))


def _check_providers(providers: dict) -> None:
    envs: dict[str, str] = {}
    for name, p in providers.items():
        if not isinstance(p, dict):
            raise ConfigError(t("config.not_a_table", name=f"providers.{name}"))
        if not PROVIDER_NAME_RE.fullmatch(name) or name == "anthropic":  # the name makes the key names
            raise ConfigError(t("config.bad_provider_name", name=name))
        _check_keys(f"providers.{name}", p, {f.name for f in fields(ProviderConfig)})
        for key in ("base_url", "model"):
            if not isinstance(p.get(key), str) or not p[key].strip():
                raise ConfigError(t("config.provider_missing", name=name, key=key))
        if not p["base_url"].strip().startswith(("http://", "https://")):
            raise ConfigError(t("config.provider_url", name=name, value=p["base_url"]))
        env = api_key_names(name)[1]  # lm-studio and lm_studio would share one key variable
        if env in envs:
            raise ConfigError(t("config.same_key_env", a=envs[env], b=name, env=env))
        envs[env] = name


def _use_language(raw: dict, exists: bool) -> str:
    """The language config.toml asks for, made the language of everything said from now on (before the other
    checks, so their errors are in it too). No `language` line: Chinese; no config file at all: the system's, the
    language a new one would be written in."""
    language = raw.get("language", i18n.DEFAULT_LANGUAGE if exists else i18n.detected_language())
    if language not in i18n.LANGUAGES:
        raise ConfigError(t("config.bad_language", value=language))
    i18n.set_language(language)
    return language


def _validate(raw: dict) -> None:
    _check_keys("", raw, TOP_LEVEL_KEYS)
    if raw.get("summary_language", "auto") not in SUMMARY_LANGUAGES:
        raise ConfigError(t("config.bad_choice", where="summary_language", value=raw["summary_language"],
                            choices=t("common.list_sep").join(SUMMARY_LANGUAGES)))
    jobs = raw.get("max_concurrent_jobs", 2)
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ConfigError(t("config.bad_jobs", value=jobs))
    batch = raw.get("read_batch_parts", 5)
    if isinstance(batch, bool) or not isinstance(batch, int) or batch < 1:
        raise ConfigError(t("config.bad_read_batch", value=batch))
    for section, cls in (("local_llm", LocalLLM), ("embed", EmbedConfig), ("media", MediaConfig)):
        _check_keys(section, _table(raw, section), {f.name for f in fields(cls)})
    _check_media(_table(raw, "media"))
    secrets = _table(raw, "secrets")
    _check_keys("secrets", secrets, {"backend"})
    if "backend" in secrets and secrets["backend"] not in SECRETS_BACKENDS:
        raise ConfigError(t("config.bad_choice", where="secrets.backend", value=secrets["backend"],
                            choices=t("common.list_sep").join(SECRETS_BACKENDS)))
    models = _table(raw, "models")
    _check_keys("models", models, TASK_NAMES)
    for task, table in models.items():
        if not isinstance(table, dict):
            raise ConfigError(t("config.not_a_table", name=f"models.{task}"))
        _check_keys(f"models.{task}", table, {"primary", "fallback"})
    providers = _table(raw, "providers")
    _check_providers(providers)
    for task, table in models.items():
        if "primary" in table:
            _check_model(f"models.{task}.primary", table["primary"], providers)
        if table.get("fallback"):  # empty: no fallback
            _check_model(f"models.{task}.fallback", table["fallback"], providers)
    channels = _table(raw, "channels")
    for name, c in channels.items():
        if not isinstance(c, dict):
            raise ConfigError(t("config.not_a_table", name=f"channels.{name}"))
        if name not in CHANNEL_NAMES:
            raise ConfigError(t("config.unknown_channel", name=name, choices=t("common.list_sep").join(sorted(CHANNEL_NAMES))))
        allowed = {f.name for f in fields(ExtensionConfig if name == EXTENSION else ChannelConfig)}
        if name == DISCORD:
            allowed = {"enabled", "owner_user_ids"}
        if name == EMAIL:
            _check_email(c)
            continue
        _check_keys(f"channels.{name}", c, allowed)
        if not isinstance(c.get("owner_user_ids", []), list):
            raise ConfigError(t("config.not_a_list", where=f"channels.{name}.owner_user_ids", value=c["owner_user_ids"]))
        if not isinstance(c.get("enabled", False), bool):
            raise ConfigError(t("config.not_a_bool", where=f"channels.{name}.enabled", value=c["enabled"]))
        if not isinstance(c.get("allowed_users", []), list):
            raise ConfigError(t("config.not_a_list", where=f"channels.{name}.allowed_users", value=c["allowed_users"]))
        port = c.get("port", 8765)
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ConfigError(t("config.bad_port", where=f"channels.{name}.port", low=1024, value=port))
        if "bind" in c:
            try:
                ipaddress.ip_address(c["bind"] if isinstance(c["bind"], str) else "")
            except ValueError:
                raise ConfigError(t("config.bad_bind", value=c["bind"])) from None


def _check_media(media: dict) -> None:
    for key, choices in (("asr_backend", ASR_BACKENDS), ("ocr_backend", OCR_BACKENDS)):
        if key in media and media[key] not in choices:
            raise ConfigError(t("config.bad_choice", where=f"media.{key}", value=media[key],
                                choices=t("common.list_sep").join(choices)))
    if media.get("x_full_text_service", "") not in X_FULL_TEXT_SERVICES:
        raise ConfigError(t("config.bad_choice", where="media.x_full_text_service", value=media["x_full_text_service"],
                            choices=t("common.list_sep").join(['""', *X_FULL_TEXT_SERVICES[1:]])))
    model = media.get("faster_whisper_model", "small")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(t("config.bad_whisper_model", value=model))


def _check_email(c: dict) -> None:
    where = "channels.email"
    _check_keys(where, c, {f.name for f in fields(EmailConfig)})
    if not isinstance(c.get("enabled", False), bool):
        raise ConfigError(t("config.not_a_bool", where=f"{where}.enabled", value=c["enabled"]))
    for key in ("imap_host", "smtp_host", "username"):
        if not isinstance(c.get(key, ""), str):
            raise ConfigError(t("config.not_text", where=f"{where}.{key}", value=c[key]))
    for key in ("imap_port", "smtp_port"):
        port = c.get(key, 993)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(t("config.bad_port", where=f"{where}.{key}", low=1, value=port))
    owners = c.get("owner_addresses", [])
    if not isinstance(owners, list) or not all(isinstance(a, str) and "@" in a for a in owners):
        raise ConfigError(t("config.bad_owners", where=f"{where}.owner_addresses", value=owners))
    poll = c.get("poll_seconds", 60)
    if isinstance(poll, bool) or not isinstance(poll, int) or poll < 15:
        raise ConfigError(t("config.bad_poll", where=f"{where}.poll_seconds", value=poll))
    servers = c.get("authserv_id", [])
    servers = [servers] if isinstance(servers, str) else servers
    if not isinstance(servers, list) or not all(isinstance(x, str) and x.strip() for x in servers):
        raise ConfigError(t("config.bad_authserv", where=f"{where}.authserv_id", value=c["authserv_id"]))
    reply = c.get("reply", "email")
    if reply not in EMAIL_REPLIES:
        raise ConfigError(t("config.bad_reply", where=f"{where}.reply", value=reply))
    if c.get("enabled"):
        for key in ("imap_host", "username") + (("smtp_host",) if reply == "email" else ()):
            if not c.get(key, "").strip():
                raise ConfigError(t("config.email_missing", where=where, key=key))
        if not owners:
            raise ConfigError(t("config.email_no_owners", where=where))


def _email(c: dict) -> EmailConfig:
    servers = c.get("authserv_id", [])
    return EmailConfig(**{k: v.strip() if isinstance(v, str) else v for k, v in c.items()
                          if k not in ("owner_addresses", "authserv_id")},
                       owner_addresses=[a.strip().lower() for a in c.get("owner_addresses", [])],
                       authserv_id=[x.strip() for x in ([servers] if isinstance(servers, str) else servers)])


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_DATA_DIR / "config.toml"
    raw = _parse(path)
    language = _use_language(raw, path.exists())
    _validate(raw)
    models = raw.get("models", {})
    channels = {
        name: ChannelConfig(
            enabled=bool(c.get("enabled", False)),
            allowed_users=[str(u) for u in c.get("owner_user_ids" if name == DISCORD else "allowed_users", [])],
        )
        for name, c in raw.get("channels", {}).items() if name not in (EXTENSION, EMAIL)
    }
    ext = raw.get("channels", {}).get(EXTENSION, {})
    data_dir = Path(raw.get("data_dir", str(DEFAULT_DATA_DIR))).expanduser()
    if not data_dir.is_absolute():  # the CLI and the launchd service start in different directories
        data_dir = (path.resolve().parent / data_dir).resolve()
    library = _library_folder(raw, path, data_dir)
    return Config(
        data_dir=data_dir,
        summarize=_task(models.get("summarize", {}), "claude:haiku"),
        answer=_task(models.get("answer", {}), "claude:sonnet"),
        local=LocalLLM(**raw.get("local_llm", {})),
        embed=EmbedConfig(**raw.get("embed", {})),
        channels=channels,
        claude_bin=raw.get("claude_bin", "claude"),
        max_concurrent_jobs=int(raw.get("max_concurrent_jobs", 2)),
        read_batch_parts=int(raw.get("read_batch_parts", 5)),
        media=MediaConfig(**raw.get("media", {})),
        extension=ExtensionConfig(enabled=bool(ext.get("enabled", False)), port=int(ext.get("port", 8765)),
                                  bind=ext.get("bind", "127.0.0.1")),
        email=_email(raw.get("channels", {}).get(EMAIL, {})),
        providers={name: ProviderConfig(p["base_url"].strip(), p["model"].strip())
                   for name, p in raw.get("providers", {}).items()},
        library_folder=library,
        secrets_backend=raw.get("secrets", {}).get("backend", "auto"),
        language=language,
        summary_language=raw.get("summary_language", language),
    )


def _library_folder(raw: dict, path: Path, data_dir: Path) -> Path | None:
    if "library_dir" not in raw:
        return None
    value = raw["library_dir"]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(t("config.bad_library_dir", value=value))
    folder = Path(value.strip()).expanduser()
    if not folder.is_absolute():
        folder = (path.resolve().parent / folder).resolve()
    if problem := library_dir_problem(data_dir, folder):
        raise ConfigError(t("config.library_dir_problem", problem=problem))
    return folder


def library_dir_problem(data_dir: Path, folder: Path) -> str | None:
    """Why folder cannot hold the library (None: it can): never around the data dir, never inside it except as
    its own library/ (the trash, backups or downloads would turn into notes)."""
    data, here = data_dir.resolve(), folder.resolve()
    if data.is_relative_to(here):
        return t("config.library_around_data")
    if here.is_relative_to(data) and here != data / "library":
        return t("config.library_inside_data")
    return None


def make_test_config(data_dir: Path) -> Config:
    """Config with defaults rooted at data_dir (used by tests)."""
    cfg = load_config(data_dir / "__no_config__.toml")
    cfg.data_dir = data_dir
    return cfg


def write_default_config(path: Path, language: str | None = None) -> bool:
    """language: the template's (its comments and its `language` line); None: the system's (P28)."""
    if path.exists():
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # usually the data dir itself: private
    text = i18n.CATALOGS[language or i18n.detected_language()]["config.template"]
    if data_dir := os.environ.get(DATA_DIR_ENV, "").strip():  # Docker: data_dir = "/data"
        text = text.replace('data_dir = "~/Octopus"', f"data_dir = {json.dumps(data_dir)}", 1)
    path.write_text(text, encoding="utf-8")
    return True


def ensure_dirs(cfg: Config) -> None:
    """Everything under data_dir is personal (library, logs, chat state): owner-only, 0700."""
    for d in (cfg.data_dir, cfg.library_dir, cfg.attachments_dir, cfg.trash_dir,
              cfg.backups_dir, cfg.logs_dir, cfg.llm_workdir, cfg.downloads_dir, cfg.incoming_dir):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg.data_dir.chmod(0o700)  # an existing data_dir keeps its old mode through mkdir
