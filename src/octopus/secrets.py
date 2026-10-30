"""Secrets, looked up in order (P22): an environment variable (OCTOPUS_<NAME>; ANTHROPIC_API_KEY), the OS
keyring under service "octopus", then <data_dir>/secrets.toml (owner-only, 0600). Values are never logged.

Which keyring (config `[secrets] backend`): "auto" is the Keychain on macOS (as before P22) and no keyring at all on
Linux (environment + file, not one D-Bus call); "keyring" on Linux is the Secret Service, opt-in, each call bounded by
a timeout, never creating or unlocking a collection, and falling back to the file with one warning; "file" never uses
a keyring. Managed secrets (MANAGED: written by Octopus itself) ignore the environment."""
from __future__ import annotations

import fcntl
import logging
import os
import re
import threading
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from octopus import host
from octopus.i18n import aliases, t

log = logging.getLogger(__name__)
SERVICE = "octopus"
FILE_NAME = "secrets.toml"
BACKENDS = ("auto", "keyring", "file")
SECRET_SERVICE_TIMEOUT = 5.0  # seconds for one Secret Service call (D-Bus has no timeout of its own)
SECRET_NAMES = {name: f"secrets.name.{name}" for name in (  # name -> the catalog key of what it is for
    "telegram_token", "feishu_app_id", "feishu_app_secret", "claude_oauth_token", "clawbot_token", "extension_token",
    "anthropic_api_key", "discord_bot_token", "email_password")}
__getattr__ = aliases({  # the old constant names, in the configured language
    "KEYCHAIN": lambda: keyring_label(), "FILE_TOO_OPEN": "secrets.file_too_open",
    "FILE_NOT_OWNED": "secrets.file_not_owned", "FILE_BROKEN": "secrets.file_broken",
    "FILE_UNWRITABLE": "secrets.file_unwritable", "FILE_NO_PERMISSIONS": "secrets.file_no_permissions",
    "BAD_VALUE": "secrets.bad_value", "NO_STORE": "secrets.no_store", "KEYRING_FALLBACK": "secrets.keyring_fallback",
    "FILE_HEADER": "secrets.file_header",
})
# written by Octopus itself (the ClawBot QR login renews it): an environment copy would go stale, so it is never read
MANAGED = {"clawbot_token"}
# the key of an OpenAI-compatible [providers.<name>] (the name rules are config.PROVIDER_NAME_RE)
PROVIDER_KEY_RE = re.compile(r"([a-z0-9][a-z0-9_-]*)_api_key")
_TOML_ERROR_AT = re.compile(r"\(at line \d+, column \d+\)|\(at end of document\)")
_TOML_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


class SecretsFileError(Exception):
    """The secrets file must not or cannot be used (open to others, malformed, unwritable); for the user."""


class KeyringUnavailable(Exception):
    """The Linux Secret Service cannot be used right now (no D-Bus, no default keyring, locked, too slow)."""


def secret_label(name: str) -> str | None:
    """What a secret is for, or None when it is not a name we store."""
    if name in SECRET_NAMES:
        return t(SECRET_NAMES[name])
    if m := PROVIDER_KEY_RE.fullmatch(name):
        return t("secrets.name.provider_key", name=m.group(1))
    return None


def keyring_label() -> str:
    """The keyring as the user knows it: the Keychain on macOS, the Secret Service on Linux."""
    return t("secrets.keychain" if host.system() == host.MAC else "secrets.keyring")


def store_name(mode: str = "auto") -> str:
    """Where this system keeps secrets ([secrets] backend mode): for "… is missing in <store>" messages."""
    if host.system() == host.MAC and mode != "file":
        return t("secrets.store_mac")
    return t("secrets.store_linux_keyring" if mode == "keyring" else "secrets.store_linux")


def keep_name(mode: str = "auto") -> str:
    """Where `secret set` keeps a secret ([secrets] backend mode): never the environment, which it only reads."""
    if host.system() == host.MAC and mode != "file":
        return t("secrets.store_mac")
    return t("secrets.keep_linux_keyring" if mode == "keyring" else "secrets.keep_linux")


def from_env(where: str | None) -> bool:
    """A "where it was found" that is an environment variable (see find)."""
    return (where or "").startswith(t("secrets.where_env", env=""))


def env_name(name: str) -> str:
    """The environment variable that holds a secret: OCTOPUS_<NAME>, or P20's ANTHROPIC_API_KEY."""
    if name == "anthropic_api_key":
        return "ANTHROPIC_API_KEY"
    return f"OCTOPUS_{name.upper().replace('-', '_')}"


def api_key_names(provider: str) -> tuple[str, str]:
    """(secret name, environment variable) of an API provider's key: "anthropic" or a [providers.<name>] name."""
    secret = "anthropic_api_key" if provider == "anthropic" else f"{provider}_api_key"
    return secret, env_name(secret)


def find_api_key(secrets: Secrets, provider: str, environ: Mapping[str, str] | None = None
                 ) -> tuple[str | None, str | None]:
    """(key, where it was found) or (None, None). The environment variable (environ, else the process's) wins: it is
    how Docker sets keys, and it overrides a stored key for one run; the stores are not read then."""
    secret, env = api_key_names(provider)
    if value := (os.environ if environ is None else environ).get(env, "").strip():
        return value, t("secrets.where_env", env=env)
    value, where = secrets.find(secret, env=False)
    if value := (value or "").strip():  # a value pasted into Keychain Access may end in a newline
        return value, where
    return None, None


def _toml_string(value: str) -> str:
    """A TOML basic string: every control character (and DEL) escaped, which json.dumps does not do for DEL."""
    out = []
    for c in value:
        if c in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[c])
        elif ord(c) < 0x20 or ord(c) == 0x7F:
            out.append(f"\\u{ord(c):04X}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def _open_default_collection() -> tuple[Any, Callable[[], None]]:
    """The Secret Service's default collection, or an error: never creates one (that is a prompt)."""
    import secretstorage

    bus = secretstorage.dbus_init()
    try:
        return secretstorage.Collection(bus), bus.close  # the "default" alias only; missing: ItemNotFoundException
    except BaseException:
        bus.close()
        raise


class SecretServiceBackend:
    """The Linux keyring when `[secrets] backend = "keyring"`: the default collection through secretstorage, used
    only while it is unlocked (never unlocked or created here: both pop up a prompt), each call in a thread bounded
    by `timeout`. Any problem is KeyringUnavailable."""

    def __init__(self, timeout: float = SECRET_SERVICE_TIMEOUT,
                 open_collection: Callable[[], tuple[Any, Callable[[], None]]] = _open_default_collection):
        self.timeout = timeout
        self._open = open_collection

    def _call(self, fn: Callable[[Any], Any]) -> Any:
        result: dict = {}

        def work() -> None:
            try:
                collection, close = self._open()
                try:
                    if collection.is_locked():
                        raise KeyringUnavailable(t("secrets.keyring_locked"))
                    result["value"] = fn(collection)
                finally:
                    close()
            except BaseException as e:  # handed to the caller's thread
                result["error"] = e

        thread = threading.Thread(target=work, name="octopus-secret-service", daemon=True)
        thread.start()
        thread.join(self.timeout)
        if thread.is_alive():  # a wedged daemon or a prompt: the thread is left behind, the caller moves on
            raise KeyringUnavailable(t("secrets.keyring_slow", seconds=f"{self.timeout:g}"))
        if "error" in result:
            e = result["error"]
            raise e if isinstance(e, KeyringUnavailable) else KeyringUnavailable(type(e).__name__)
        return result.get("value")

    @staticmethod
    def _attrs(service: str, name: str) -> dict[str, str]:
        return {"service": service, "username": name}  # what keyring and `secret-tool` use too

    def get_password(self, service: str, name: str) -> str | None:
        def get(col):
            items = list(col.search_items(self._attrs(service, name)))
            return items[0].get_secret().decode("utf-8") if items else None
        return self._call(get)

    def set_password(self, service: str, name: str, value: str) -> None:
        self._call(lambda col: col.create_item(f"Octopus {name}", self._attrs(service, name), value.encode("utf-8"),
                                               replace=True))

    def delete_password(self, service: str, name: str) -> None:
        def delete(col):
            for item in col.search_items(self._attrs(service, name)):
                item.delete()
        self._call(delete)


class Secrets:
    def __init__(self, backend=None, file: Path | None = None, environ: Mapping[str, str] | None = None,
                 mode: str = "auto"):
        """backend: a keyring-like object, False for none, None to pick one from `mode` ([secrets] backend) and the
        system. file: <data_dir>/secrets.toml. environ: where OCTOPUS_<NAME> values are read (default: the process)."""
        self._backend = backend
        self.mode = mode
        self.file = file
        self.environ = os.environ if environ is None else environ
        self._warned: tuple | None = None  # (file version, message) last logged: said once per version

    @property
    def backend(self):
        """The keyring, or None when there is none to use (decided on first use)."""
        if self._backend is None:
            if self.mode == "file":
                self._backend = False
            elif host.system() == host.MAC:
                import keyring as backend

                self._backend = backend
            else:  # Linux: only when asked for, never in auto
                self._backend = SecretServiceBackend() if self.mode == "keyring" else False
        return self._backend or None

    @property
    def file_label(self) -> str:
        return t("secrets.where_file", path=self.file)

    @staticmethod
    def _check(name: str) -> None:
        if secret_label(name) is None:
            raise KeyError(name)

    def _env(self, name: str) -> str | None:
        if name in MANAGED:
            return None
        return self.environ.get(env_name(name), "").strip() or None

    def _keyring_failed(self, e: KeyringUnavailable) -> None:
        """The opt-in Linux keyring did not answer: this instance uses the file from now on, said once."""
        self._backend = False
        log.warning("%s", t("secrets.keyring_fallback", why=e, path=self.file))

    def _version(self) -> tuple | None:
        try:
            st = os.stat(self.file)
        except (OSError, TypeError):
            return None
        return st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode, st.st_uid

    def _say_file_problem(self, e: SecretsFileError) -> None:
        if (key := (self._version(), str(e))) != self._warned:  # once per file version, not on every lookup
            self._warned = key
            log.warning("%s", e)

    def _read_file(self) -> dict[str, str]:
        """The file's values ({} when there is none); SecretsFileError when it must not or cannot be read."""
        if self.file is None:
            return {}
        try:
            st = os.stat(self.file)
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise SecretsFileError(t("secrets.file_broken", path=self.file, error=e.strerror or type(e).__name__)) from None
        if st.st_uid != os.geteuid():  # like ssh: someone else's file could have been written by them
            raise SecretsFileError(t("secrets.file_not_owned", path=self.file, uid=os.geteuid()))
        if st.st_mode & 0o077:  # like ssh with a private key: others can read it, so it is not used
            raise SecretsFileError(t("secrets.file_too_open", path=self.file))
        try:
            data = tomllib.loads(self.file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:  # only where: tomllib quotes an illegal character of the value
            where = m.group(0) if (m := _TOML_ERROR_AT.search(str(e))) else "TOML"
            raise SecretsFileError(t("secrets.file_broken", path=self.file,
                                     error=t("secrets.file_bad_toml", where=where))) from None
        except (OSError, UnicodeDecodeError) as e:
            raise SecretsFileError(t("secrets.file_broken", path=self.file, error=type(e).__name__)) from None
        return {k: v for k, v in data.items() if isinstance(v, str)}

    def _write_file(self, values: dict[str, str]) -> None:
        text = t("secrets.file_header") + "".join(f"{_toml_string(k)} = {_toml_string(v)}\n" for k, v in sorted(values.items()))
        try:
            data = text.encode("utf-8")
        except UnicodeError:
            raise SecretsFileError(t("secrets.bad_value")) from None
        tmp = self.file.with_name(f".{self.file.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
        try:
            # a fresh name, never followed through a link, owner-only from the first byte
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.file)
            finally:
                tmp.unlink(missing_ok=True)
            ignores_modes = bool(os.stat(self.file).st_mode & 0o077)  # a CIFS/SMB share or FUSE mount
            if ignores_modes:
                self.file.unlink(missing_ok=True)
        except OSError as e:
            raise SecretsFileError(t("secrets.file_unwritable", path=self.file,
                                     error=e.strerror or type(e).__name__)) from None
        if ignores_modes:
            raise SecretsFileError(t("secrets.file_no_permissions", path=self.file))

    def _update(self, change: Callable[[dict[str, str]], bool]) -> None:
        """Read, change, write the file under an exclusive lock (the service's ClawBot login and a `secret set` in
        another shell must not drop each other's key). change returns whether to write."""
        try:
            self.file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock = os.open(self.file.with_name(self.file.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as e:
            raise SecretsFileError(t("secrets.file_unwritable", path=self.file,
                                     error=e.strerror or type(e).__name__)) from None
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            values = self._read_file()
            if change(values):
                self._write_file(values)
        finally:
            os.close(lock)  # releases the lock

    def _from_file(self, name: str) -> str | None:
        try:
            return self._read_file().get(name)
        except SecretsFileError as e:
            self._say_file_problem(e)
            return None

    def find(self, name: str, env: bool = True) -> tuple[str | None, str | None]:
        """(value, where it was found) or (None, None); env=False skips the environment (the caller read it)."""
        self._check(name)
        if env and (value := self._env(name)):
            return value, t("secrets.where_env", env=env_name(name))
        if self.backend is not None:
            try:
                value = self.backend.get_password(SERVICE, name)
            except KeyringUnavailable as e:
                self._keyring_failed(e)
                value = None
            except Exception as e:  # locked keychain, denied access...: treat as not set, but say why
                log.warning("cannot read secret %s from Keychain: %s", name, e)
                value = None
            if value is not None:
                return value, f"{keyring_label()} {name}"
        if (value := self._from_file(name)) is not None:
            return value, self.file_label
        return None, None

    def get(self, name: str) -> str | None:
        return self.find(name)[0]

    def set(self, name: str, value: str) -> str:
        """Saves to the keyring when there is one, else to the file; returns where (the keychain's label or the file).
        Keychain errors propagate; a file problem is a SecretsFileError."""
        self._check(name)
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise SecretsFileError(t("secrets.bad_value")) from None
        if self.backend is not None:
            try:
                self.backend.set_password(SERVICE, name, value)
                return keyring_label()
            except KeyringUnavailable as e:
                self._keyring_failed(e)
        if self.file is None:
            raise SecretsFileError(t("secrets.no_store"))

        def put(values: dict[str, str]) -> bool:
            values[name] = value
            return True

        self._update(put)
        return self.file_label

    def delete(self, name: str) -> None:
        """A missing secret is fine; any other Keychain error (locked, denied...) propagates."""
        self._check(name)
        if self.backend is not None:
            from keyring.errors import PasswordDeleteError

            try:
                self.backend.delete_password(SERVICE, name)
            except PasswordDeleteError:
                pass
            except KeyringUnavailable as e:
                self._keyring_failed(e)
        if self.file is not None and self.file.exists():
            self._update(lambda values: values.pop(name, None) is not None)

    def status(self) -> dict[str, tuple[bool | None, str | None]]:
        """name → (state, source). State: True = set, False = not set, None = could not be read (locked or denied
        Keychain, unusable file). Source: the environment variable, the keychain or
        the secrets file (catalog wording); never the value."""
        try:
            stored, file_ok = self._read_file(), True
        except SecretsFileError as e:
            self._say_file_problem(e)
            stored, file_ok = {}, False
        out: dict[str, tuple[bool | None, str | None]] = {}
        for name in SECRET_NAMES:
            if self._env(name):
                out[name] = (True, t("secrets.where_env", env=env_name(name)))
                continue
            in_keyring: bool | None = False
            if self.backend is not None:
                try:
                    in_keyring = self.backend.get_password(SERVICE, name) is not None
                except KeyringUnavailable as e:
                    self._keyring_failed(e)
                except Exception as e:
                    log.warning("cannot read secret %s from Keychain: %s", name, e)
                    in_keyring = None
            if in_keyring:
                out[name] = (True, keyring_label())
            elif name in stored:
                out[name] = (True, self.file_label)
            else:
                out[name] = (None if in_keyring is None or not file_ok else False, None)
        return out
