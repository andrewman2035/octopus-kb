import stat

import keyring.errors
import pytest

from octopus.secrets import SECRET_NAMES, SERVICE, Secrets


class MemoryBackend:
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise keyring.errors.PasswordDeleteError(name)  # what keyring raises for a missing item
        del self.store[(service, name)]


def test_set_get_delete_status():
    b = MemoryBackend()
    s = Secrets(b)
    assert s.get("telegram_token") is None
    s.set("telegram_token", "123:abc")
    assert b.store[(SERVICE, "telegram_token")] == "123:abc"
    assert s.get("telegram_token") == "123:abc"
    assert s.status()["telegram_token"] == (True, "钥匙串") and s.status()["feishu_app_id"] == (False, None)
    s.delete("telegram_token")
    s.delete("telegram_token")  # deleting a missing secret is fine
    assert s.get("telegram_token") is None


def test_unknown_name_rejected():
    with pytest.raises(KeyError):
        Secrets(MemoryBackend()).set("password", "x")
    assert set(SECRET_NAMES) == {"telegram_token", "feishu_app_id", "feishu_app_secret",
                                 "claude_oauth_token", "clawbot_token", "extension_token", "anthropic_api_key",
                                 "discord_bot_token", "email_password"}


def test_backend_errors_read_as_missing():
    class Broken(MemoryBackend):
        def get_password(self, service, name):
            raise RuntimeError("locked")

    assert Secrets(Broken()).get("telegram_token") is None


# ---- P4 Task 5: secrets robustness ----

def test_status_reports_unreadable_as_none():
    class Locked(MemoryBackend):
        def get_password(self, service, name):
            if name == "clawbot_token":
                raise keyring.errors.KeyringLocked("locked")
            return super().get_password(service, name)

    b = Locked()
    b.store[(SERVICE, "telegram_token")] = "t"
    status = Secrets(b).status()
    assert status["telegram_token"][0] is True and status["feishu_app_id"][0] is False
    assert status["clawbot_token"][0] is None  # locked/denied: unknown, not "not set"


def test_delete_swallows_only_not_found():
    class Denied(MemoryBackend):
        def delete_password(self, service, name):
            raise keyring.errors.KeyringLocked("denied")

    Secrets(MemoryBackend()).delete("telegram_token")  # missing: fine
    with pytest.raises(keyring.errors.KeyringLocked):
        Secrets(Denied()).delete("telegram_token")


# ---- P20: API keys ----

def test_api_key_names():
    from octopus.secrets import api_key_names

    assert api_key_names("anthropic") == ("anthropic_api_key", "ANTHROPIC_API_KEY")
    assert api_key_names("deepseek") == ("deepseek_api_key", "OCTOPUS_DEEPSEEK_API_KEY")
    assert api_key_names("lm-studio") == ("lm-studio_api_key", "OCTOPUS_LM_STUDIO_API_KEY")


def test_provider_key_names_are_accepted_others_still_rejected():
    from octopus.secrets import secret_label

    s = Secrets(MemoryBackend())
    s.set("deepseek_api_key", "sk-d")
    assert s.get("deepseek_api_key") == "sk-d"
    assert "deepseek" in secret_label("deepseek_api_key")
    assert "Anthropic" in secret_label("anthropic_api_key")
    for bad in ("password", "_api_key", "DeepSeek_api_key", "x api_key"):
        assert secret_label(bad) is None
        with pytest.raises(KeyError):
            s.set(bad, "x")


def test_find_api_key_env_wins_over_keychain():
    from octopus.secrets import find_api_key

    b = MemoryBackend()
    s = Secrets(b)
    assert find_api_key(s, "anthropic", environ={}) == (None, None)
    s.set("anthropic_api_key", "sk-chain")
    assert find_api_key(s, "anthropic", environ={}) == ("sk-chain", "钥匙串 anthropic_api_key")
    env = {"ANTHROPIC_API_KEY": "sk-env"}
    assert find_api_key(s, "anthropic", environ=env) == ("sk-env", "环境变量 ANTHROPIC_API_KEY")
    assert find_api_key(s, "anthropic", environ={"ANTHROPIC_API_KEY": "  "})[0] == "sk-chain"  # blank: not set
    s.set("deepseek_api_key", "sk-d")
    assert find_api_key(s, "deepseek", environ=env) == ("sk-d", "钥匙串 deepseek_api_key")
    assert find_api_key(s, "deepseek", environ={"OCTOPUS_DEEPSEEK_API_KEY": "sk-de"})[0] == "sk-de"


def test_find_api_key_skips_the_keychain_when_the_environment_has_it():
    from octopus.secrets import find_api_key

    class NoKeychain(MemoryBackend):
        def get_password(self, service, name):
            raise AssertionError("Keychain read")

    assert find_api_key(Secrets(NoKeychain()), "anthropic", environ={"ANTHROPIC_API_KEY": "k"})[0] == "k"


def test_find_api_key_strips_the_keychain_value():
    from octopus.secrets import find_api_key

    s = Secrets(MemoryBackend())
    s.set("anthropic_api_key", " sk-chain\n")
    assert find_api_key(s, "anthropic", environ={}) == ("sk-chain", "钥匙串 anthropic_api_key")
    s.set("anthropic_api_key", " \n")
    assert find_api_key(s, "anthropic", environ={}) == (None, None)


def test_tests_never_reach_the_real_keyring():
    import keyring

    assert type(keyring.get_keyring()).__name__ == "NoRealKeyring"


# ---- P22: environment → keyring → <data_dir>/secrets.toml ----

import os
import threading
import time


def test_env_names():
    from octopus.secrets import env_name

    assert env_name("telegram_token") == "OCTOPUS_TELEGRAM_TOKEN"
    assert env_name("anthropic_api_key") == "ANTHROPIC_API_KEY"  # P20's name
    assert env_name("deepseek_api_key") == "OCTOPUS_DEEPSEEK_API_KEY"
    assert env_name("lm-studio_api_key") == "OCTOPUS_LM_STUDIO_API_KEY"


def test_the_environment_wins_over_the_keyring_and_the_file(tmp_path):
    b = MemoryBackend()
    b.store[(SERVICE, "telegram_token")] = "from-keychain"
    s = Secrets(b, file=tmp_path / "secrets.toml", environ={"OCTOPUS_TELEGRAM_TOKEN": " from-env "})
    assert s.get("telegram_token") == "from-env"
    assert s.find("telegram_token") == ("from-env", "环境变量 OCTOPUS_TELEGRAM_TOKEN")
    assert s.find("telegram_token", env=False) == ("from-keychain", "钥匙串 telegram_token")
    assert s.find("feishu_app_id") == (None, None)
    s = Secrets(b, environ={"OCTOPUS_TELEGRAM_TOKEN": "  "})  # blank: not set
    assert s.find("telegram_token") == ("from-keychain", "钥匙串 telegram_token")
    assert s.status()["telegram_token"] == (True, "钥匙串")  # `secret list` shows the short source


def test_managed_secrets_ignore_the_environment():
    from octopus.secrets import MANAGED

    assert MANAGED == {"clawbot_token"}  # written by Octopus itself (the QR login), never configuration
    b = MemoryBackend()
    b.store[(SERVICE, "clawbot_token")] = "fresh-login"
    s = Secrets(b, environ={"OCTOPUS_CLAWBOT_TOKEN": "stale"})
    assert s.get("clawbot_token") == "fresh-login"
    assert s.status()["clawbot_token"] == (True, "钥匙串")


def test_without_a_keyring_secrets_go_to_a_private_file(tmp_path):
    f = tmp_path / "data" / "secrets.toml"
    s = Secrets(False, file=f, environ={})
    assert s.get("telegram_token") is None
    assert s.set("telegram_token", '12:a"b\\c') == f"密钥文件 {f}"
    assert s.set("deepseek_api_key", "sk-1") == f"密钥文件 {f}"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert s.get("telegram_token") == '12:a"b\\c' and s.get("deepseek_api_key") == "sk-1"
    assert s.find("telegram_token")[1] == f"密钥文件 {f}"
    assert Secrets(False, file=f, environ={}).status()["telegram_token"] == (True, f"密钥文件 {f}")
    s.delete("telegram_token")
    s.delete("telegram_token")  # missing: fine
    assert s.get("telegram_token") is None and s.get("deepseek_api_key") == "sk-1"
    assert sorted(p.name for p in f.parent.iterdir()) == ["secrets.toml", "secrets.toml.lock"]  # no tmp left


def test_every_character_round_trips(tmp_path):
    import tomllib

    f = tmp_path / "secrets.toml"
    s = Secrets(False, file=f, environ={})
    value = "".join(chr(c) for c in range(0x20)) + '\x7f"\\ 中文😀 \u0085é'
    s.set("telegram_token", value)
    assert tomllib.loads(f.read_text(encoding="utf-8"))["telegram_token"] == value
    assert Secrets(False, file=f, environ={}).get("telegram_token") == value


def test_a_lone_surrogate_is_refused_in_chinese(tmp_path):
    from octopus.secrets import SecretsFileError

    f = tmp_path / "secrets.toml"
    s = Secrets(False, file=f, environ={})
    with pytest.raises(SecretsFileError, match="无效"):
        s.set("telegram_token", "ab\ud800c")
    assert not f.exists() and [p.name for p in tmp_path.iterdir()] in ([], ["secrets.toml.lock"])


def test_a_file_others_can_read_is_refused_with_a_hint_once(tmp_path, caplog):
    from octopus.secrets import SecretsFileError

    f = tmp_path / "secrets.toml"
    f.write_text('telegram_token = "t"\n', encoding="utf-8")
    f.chmod(0o644)
    s = Secrets(False, file=f, environ={})
    assert s.get("telegram_token") is None
    assert s.get("telegram_token") is None and s.get("feishu_app_id") is None
    assert caplog.text.count(f"chmod 600 {f}") == 1 and '"t"' not in caplog.text  # once per file version
    assert s.status()["telegram_token"] == (None, None)  # unknown, not "not set"
    with pytest.raises(SecretsFileError, match="chmod 600"):
        s.set("feishu_app_id", "x")
    assert f.read_text(encoding="utf-8") == 'telegram_token = "t"\n'  # left alone
    f.chmod(0o640)  # a new version of the file: said again
    s.get("telegram_token")
    assert caplog.text.count(f"chmod 600 {f}") == 2
    f.chmod(0o600)
    assert s.get("telegram_token") == "t"


def test_a_file_owned_by_someone_else_is_refused(tmp_path, caplog, monkeypatch):
    f = tmp_path / "secrets.toml"
    f.write_text('telegram_token = "t"\n', encoding="utf-8")
    f.chmod(0o600)
    monkeypatch.setattr(os, "geteuid", lambda: f.stat().st_uid + 1)
    assert Secrets(False, file=f, environ={}).get("telegram_token") is None
    assert "chown" in caplog.text and str(f) in caplog.text


def test_a_filesystem_that_ignores_chmod_is_said_and_nothing_is_left(tmp_path, monkeypatch):
    from octopus.secrets import SecretsFileError

    real_stat = os.stat

    def loose(path, *a, **kw):  # a CIFS mount: every file shows 0777
        st = real_stat(path, *a, **kw)
        if str(path).endswith("secrets.toml"):
            return os.stat_result((st.st_mode | 0o777,) + tuple(st)[1:])
        return st

    monkeypatch.setattr(os, "stat", loose)
    f = tmp_path / "secrets.toml"
    with pytest.raises(SecretsFileError, match="不支持权限"):
        Secrets(False, file=f, environ={}).set("telegram_token", "t")
    assert not f.exists()


def test_a_file_that_vanishes_right_after_the_write_is_said_in_chinese(tmp_path, monkeypatch):
    """P22 review nit: the permission check after the write is inside the error handling (a network mount that
    drops the file between the replace and the stat): a SecretsFileError, never a raw OSError."""
    from octopus.secrets import SecretsFileError

    real_stat, f = os.stat, tmp_path / "secrets.toml"
    written = []
    real_replace = os.replace

    def replace(src, dst, *a, **kw):
        real_replace(src, dst, *a, **kw)
        written.append(dst)

    def gone(path, *a, **kw):
        if written and str(path) == str(f):
            raise OSError(5, "Input/output error")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "stat", gone)
    with pytest.raises(SecretsFileError, match="没能写入密钥文件") as exc:
        Secrets(False, file=f, environ={}).set("telegram_token", "t")
    assert "Input/output error" in str(exc.value)


def test_a_symlink_where_the_tmp_file_goes_is_not_written_through(tmp_path):
    f = tmp_path / "secrets.toml"
    target = tmp_path / "elsewhere"
    for name in ("secrets.toml.tmp", f".secrets.toml.{os.getpid()}.tmp"):
        (tmp_path / name).symlink_to(target)
    Secrets(False, file=f, environ={}).set("telegram_token", "t")
    assert not target.exists() and not f.is_symlink()


def test_writers_take_turns(tmp_path):
    f = tmp_path / "secrets.toml"
    names = [f"p{i}_api_key" for i in range(12)]
    threads = [threading.Thread(target=Secrets(False, file=f, environ={}).set, args=(n, n)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = Secrets(False, file=f, environ={})
    assert [s.get(n) for n in names] == names  # no update lost


def test_a_broken_file_is_said_without_its_contents(tmp_path, caplog):
    f = tmp_path / "secrets.toml"
    f.write_text('telegram_token = "sk-unterminated\n', encoding="utf-8")
    f.chmod(0o600)
    assert Secrets(False, file=f, environ={}).get("telegram_token") is None
    assert str(f) in caplog.text and "sk-unterminated" not in caplog.text
    f.write_text('telegram_token = "a\x7fz"\n', encoding="utf-8")  # tomllib quotes the bad character
    assert Secrets(False, file=f, environ={}).get("telegram_token") is None
    assert "\x7f" not in caplog.text and "\\x7f" not in caplog.text


def test_values_are_never_logged(tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    f = tmp_path / "secrets.toml"
    s = Secrets(False, file=f, environ={"OCTOPUS_FEISHU_APP_ID": "env-value"})
    s.set("telegram_token", "file-value")
    s.get("telegram_token"), s.get("feishu_app_id"), s.status()
    assert "file-value" not in caplog.text and "env-value" not in caplog.text


def test_no_keyring_and_no_file_cannot_save():
    from octopus.secrets import SecretsFileError

    with pytest.raises(SecretsFileError):
        Secrets(False, environ={}).set("telegram_token", "x")


def test_with_a_keyring_set_goes_there_and_the_file_is_still_read(tmp_path):
    f = tmp_path / "secrets.toml"
    f.write_text('feishu_app_id = "from-file"\n', encoding="utf-8")
    f.chmod(0o600)
    b = MemoryBackend()
    s = Secrets(b, file=f, environ={})
    assert s.set("telegram_token", "t") == "钥匙串"
    assert b.store[(SERVICE, "telegram_token")] == "t" and "telegram_token" not in f.read_text(encoding="utf-8")
    assert s.find("feishu_app_id") == ("from-file", f"密钥文件 {f}")


def test_find_api_key_reads_the_file_too_and_uses_only_the_given_environment(tmp_path, monkeypatch):
    from octopus.secrets import find_api_key

    f = tmp_path / "secrets.toml"
    s = Secrets(False, file=f, environ={})
    s.set("anthropic_api_key", " sk-file ")
    assert find_api_key(s, "anthropic", environ={}) == ("sk-file", f"密钥文件 {f}")
    assert find_api_key(s, "anthropic", environ={"ANTHROPIC_API_KEY": "sk-env"})[0] == "sk-env"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-process")
    assert find_api_key(Secrets(False, file=f), "anthropic", environ={})[0] == "sk-file"  # the injected one only


# ---- which store: secrets.backend = auto | keyring | file ----

def _linux(monkeypatch):
    from octopus import host

    monkeypatch.setattr(host, "system", lambda: host.LINUX)


def test_auto_is_the_keychain_on_a_mac_and_the_file_on_linux(tmp_path, monkeypatch):
    import octopus.secrets as mod

    f = tmp_path / "secrets.toml"
    assert Secrets(file=f, environ={}).backend is not None  # the Keychain, as before P22
    monkeypatch.setattr(mod, "SecretServiceBackend", lambda **kw: pytest.fail("D-Bus touched"))
    _linux(monkeypatch)
    s = Secrets(file=f, environ={"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"})
    assert s.backend is None and s.set("telegram_token", "t") == f"密钥文件 {f}"


def test_file_mode_never_uses_the_keychain_even_on_a_mac(tmp_path):
    f = tmp_path / "secrets.toml"
    s = Secrets(file=f, environ={}, mode="file")
    assert s.backend is None and s.set("telegram_token", "t") == f"密钥文件 {f}"


class FakeCollection:
    def __init__(self, locked=False, delay=0.0):
        self.locked, self.delay, self.items = locked, delay, {}

    def is_locked(self):
        time.sleep(self.delay)
        return self.locked

    def search_items(self, attrs):
        item = self.items.get((attrs["service"], attrs["username"]))
        return [item] if item else []

    def create_item(self, label, attrs, secret, replace=True):
        self.items[(attrs["service"], attrs["username"])] = FakeItem(self, (attrs["service"], attrs["username"]), secret)


class FakeItem:
    def __init__(self, col, key, secret):
        self.col, self.key, self.secret = col, key, secret

    def get_secret(self):
        return self.secret

    def delete(self):
        del self.col.items[self.key]


def test_keyring_mode_on_linux_uses_an_unlocked_secret_service(tmp_path, monkeypatch):
    from octopus.secrets import SecretServiceBackend

    _linux(monkeypatch)
    col = FakeCollection()
    backend = SecretServiceBackend(open_collection=lambda: (col, lambda: None))
    s = Secrets(backend, file=tmp_path / "secrets.toml", environ={})
    assert s.set("telegram_token", "t") == "钥匙串"
    assert col.items[(SERVICE, "telegram_token")].secret == b"t"
    assert s.find("telegram_token") == ("t", "钥匙串 telegram_token")
    s.delete("telegram_token")
    s.delete("telegram_token")
    assert s.get("telegram_token") is None


@pytest.mark.parametrize("col", [FakeCollection(locked=True), FakeCollection(delay=1.0), None])
def test_a_locked_slow_or_missing_secret_service_falls_back_to_the_file_with_one_warning(tmp_path, monkeypatch,
                                                                                       caplog, col):
    from octopus.secrets import SecretServiceBackend

    _linux(monkeypatch)

    def open_collection():
        if col is None:
            raise RuntimeError("no D-Bus")
        return col, lambda: None

    f = tmp_path / "secrets.toml"
    s = Secrets(SecretServiceBackend(timeout=0.2, open_collection=open_collection), file=f, environ={})
    started = time.monotonic()
    assert s.set("telegram_token", "t") == f"密钥文件 {f}"  # never blocks on an unlock prompt
    assert s.get("telegram_token") == "t" and s.get("feishu_app_id") is None
    assert time.monotonic() - started < 0.9
    assert caplog.text.count("密钥文件") == 1
    assert (col is None or not col.items)


def test_the_real_secret_service_path_never_creates_or_unlocks(monkeypatch):
    """The default alias only: a missing one is "no keyring", never a create-a-keyring dialog."""
    import sys
    import types

    from octopus.secrets import KeyringUnavailable, SecretServiceBackend

    calls = []
    fake = types.ModuleType("secretstorage")
    fake.exceptions = types.SimpleNamespace(ItemNotFoundException=LookupError)

    class Bus:
        def close(self):
            calls.append("close")

    fake.dbus_init = lambda: Bus()

    def missing(bus, path=None):
        raise LookupError("no default alias")

    fake.Collection = missing
    fake.get_default_collection = lambda *a, **k: pytest.fail("may create a collection (prompt)")
    fake.create_collection = lambda *a, **k: pytest.fail("creates a collection (prompt)")
    monkeypatch.setitem(sys.modules, "secretstorage", fake)
    with pytest.raises(KeyringUnavailable):
        SecretServiceBackend(timeout=1).get_password(SERVICE, "telegram_token")
    assert calls == ["close"]


# ---- integration (P22 + P23): Discord and email secrets go through the same lookup ----

def test_discord_and_email_secrets_come_from_the_environment_or_the_file_on_linux(tmp_path, monkeypatch):
    """On Linux / Docker: OCTOPUS_DISCORD_BOT_TOKEN / OCTOPUS_EMAIL_PASSWORD, else secrets.toml; both are set by the
    owner (not MANAGED), so the environment wins as for every other token."""
    from octopus.bootstrap import build_channels
    from octopus.config import ChannelConfig, EmailConfig, make_test_config
    from octopus.secrets import MANAGED, env_name

    _linux(monkeypatch)
    assert {"discord_bot_token", "email_password"} <= set(SECRET_NAMES)
    assert not {"discord_bot_token", "email_password"} & MANAGED
    assert env_name("discord_bot_token") == "OCTOPUS_DISCORD_BOT_TOKEN"
    assert env_name("email_password") == "OCTOPUS_EMAIL_PASSWORD"
    f = tmp_path / "secrets.toml"
    s = Secrets(file=f, environ={"OCTOPUS_DISCORD_BOT_TOKEN": "d-env"})
    assert s.set("email_password", "pw-file") == f"密钥文件 {f}"
    assert s.find("discord_bot_token") == ("d-env", "环境变量 OCTOPUS_DISCORD_BOT_TOKEN")
    assert s.find("email_password") == ("pw-file", f"密钥文件 {f}")
    cfg = make_test_config(tmp_path)
    cfg.channels["discord"] = ChannelConfig(enabled=True, allowed_users=["1"])
    cfg.email = EmailConfig(enabled=True, imap_host="imap.gmail.com", smtp_host="smtp.gmail.com",
                            username="box@example.com", owner_addresses=["me@example.com"])
    channels, problems = build_channels(cfg, s, notify=None)
    assert problems == [] and {"discord", "email"} <= set(channels)
    assert channels["email"].password == "pw-file"
