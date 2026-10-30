import logging

logging.getLogger("jieba").setLevel(logging.WARNING)

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """No test may reach the real ~/Octopus, ~/Library or the real HOME: give each one a throwaway HOME."""
    home = tmp_path_factory.mktemp("pytest-home") / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    import octopus.cli
    import octopus.config

    monkeypatch.setattr(octopus.config, "DEFAULT_DATA_DIR", home / "Octopus")
    monkeypatch.setattr(octopus.cli, "DEFAULT_DATA_DIR", home / "Octopus")


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch):
    """No test may read or write the real Keychain, nor pick up the developer's API keys from the environment.
    Secrets swallows read errors, so a touch is recorded here and fails the test at teardown."""
    import os

    import keyring
    from keyring.backend import KeyringBackend

    touched: list[str] = []

    class NoRealKeyring(KeyringBackend):
        priority = 1

        def get_password(self, service, username):
            touched.append(f"get {username}")
            raise RuntimeError("test touched the real keyring")

        def set_password(self, service, username, password):
            touched.append(f"set {username}")
            raise RuntimeError("test touched the real keyring")

        def delete_password(self, service, username):
            touched.append(f"delete {username}")
            raise RuntimeError("test touched the real keyring")

    keyring.set_keyring(NoRealKeyring())
    for name in list(os.environ):  # API keys, OCTOPUS_<SECRET> values, OCTOPUS_DATA_DIR (P22)
        if name == "ANTHROPIC_API_KEY" or name.startswith("OCTOPUS_"):
            monkeypatch.delenv(name)
    yield
    if touched:
        pytest.fail(f"test reached the real keyring: {touched}")


@pytest.fixture(autouse=True)
def _mac_by_default(monkeypatch):
    """Tests describe the macOS behaviour wherever they run (Linux CI, Docker); the Linux paths are tested by
    pinning host.system to LINUX in the tests that are about them."""
    from octopus import host

    monkeypatch.setattr(host, "system", lambda: host.MAC)


@pytest.fixture(autouse=True)
def _chinese_by_default():
    """Every test starts in Chinese (the default) and leaves it so: an English test, or a config.toml with
    language = "en" loaded by one, must not change the language of the tests after it (P25)."""
    from octopus import i18n

    i18n.set_language("zh")
    yield
    i18n.set_language("zh")


@pytest.fixture(autouse=True)
def _chinese_system(monkeypatch):
    """A new config, and everything said while there is none, follow the system's language (P28): tests never read
    the developer's locale or the real `defaults` (calling it fails the test); they run on a Chinese system (the
    language the tests before P28 expect). Tests about the detection put their own in; the real detection's
    per-process cache starts empty in each test."""
    from octopus import i18n

    i18n.detected_language.cache_clear()  # the real one, for tests that put it back
    monkeypatch.setattr(i18n, "detected_language", lambda: "zh")
    monkeypatch.setattr(i18n, "mac_ui_language", lambda run=None: pytest.fail("a test ran the real `defaults`"))
