"""P22: Octopus on Linux (natively and in Docker); the macOS paths are unchanged."""
import pytest
from fakes import FakeEmbedder

from octopus import host
from octopus.bootstrap import build_app
from octopus.config import make_test_config


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(host, "system", lambda: host.LINUX)


def test_build_app_picks_the_linux_media_backends(tmp_path, linux, monkeypatch):
    from octopus.media import asr, ocr

    cfg = make_test_config(tmp_path)
    app = build_app(cfg, {}, embedder=FakeEmbedder())
    try:
        assert isinstance(app.media.transcribe.__self__, asr.FasterWhisperTranscriber)
        assert app.media.transcribe.__self__.model == "small"
        seen = []
        monkeypatch.setitem(ocr.RECOGNIZERS, "rapidocr", lambda img: seen.append(img) or [("字", 1.0, None)])
        assert app.media.ocr("a.png") == "字" and seen == ["a.png"]
        assert app.media.pdf.keywords["ocr"] is app.media.ocr  # a scanned PDF's pages go to the same OCR
    finally:
        app.close()


def test_build_app_keeps_the_mac_backends(tmp_path, monkeypatch):
    from octopus.media import asr, ocr

    cfg = make_test_config(tmp_path)
    app = build_app(cfg, {}, embedder=FakeEmbedder())
    try:
        assert isinstance(app.media.transcribe.__self__, asr.WhisperTranscriber)
        assert app.media.transcribe.__self__.model == "mlx-community/whisper-large-v3-turbo"
        monkeypatch.setitem(ocr.RECOGNIZERS, "ocrmac", lambda img: [("mac", 1.0, None)])
        assert app.media.ocr("a.png") == "mac"
    finally:
        app.close()


def test_a_backend_set_in_the_config_wins(tmp_path, linux, monkeypatch):
    from octopus.media import asr, ocr

    cfg = make_test_config(tmp_path)
    cfg.media.asr_backend, cfg.media.ocr_backend = "mlx", "ocrmac"
    app = build_app(cfg, {}, embedder=FakeEmbedder())
    try:
        assert isinstance(app.media.transcribe.__self__, asr.WhisperTranscriber)
        monkeypatch.setitem(ocr.RECOGNIZERS, "ocrmac", lambda img: [("mac", 1.0, None)])
        assert app.media.ocr("a.png") == "mac"
    finally:
        app.close()


# ---- secrets: no Keychain on a Linux server or in Docker ----

def _config(tmp_path, extra=""):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path / "data"}"\n{extra}', encoding="utf-8")
    return cfg


def test_secret_set_on_linux_writes_the_private_file_and_says_so(tmp_path, linux, monkeypatch, capsys):
    import stat

    import octopus.cli as cli_mod
    from octopus.cli import main

    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt: "123:abc")
    cfg = _config(tmp_path)
    assert main(["--config", str(cfg), "secret", "set", "telegram_token"]) == 0
    f = tmp_path / "data" / "secrets.toml"
    out = capsys.readouterr().out
    assert f"已保存到密钥文件 {f}：telegram_token" in out and "123:abc" not in out
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert main(["--config", str(cfg), "secret", "list"]) == 0
    out = capsys.readouterr().out
    assert f"telegram_token：已设置（密钥文件 {f}）" in out and "123:abc" not in out


def test_secret_set_says_when_the_environment_wins(tmp_path, linux, monkeypatch, capsys):
    import octopus.cli as cli_mod
    from octopus.cli import main

    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt: "123:abc")
    monkeypatch.setenv("OCTOPUS_TELEGRAM_TOKEN", "456:def")
    assert main(["--config", str(_config(tmp_path)), "secret", "set", "telegram_token"]) == 0
    out = capsys.readouterr().out
    assert "OCTOPUS_TELEGRAM_TOKEN" in out and "优先" in out and "456:def" not in out


def test_secret_set_reports_a_file_open_to_others(tmp_path, linux, monkeypatch, capsys):
    import octopus.cli as cli_mod
    from octopus.cli import main

    f = tmp_path / "data" / "secrets.toml"
    f.parent.mkdir()
    f.write_text("", encoding="utf-8")
    f.chmod(0o644)
    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt: "123:abc")
    assert main(["--config", str(_config(tmp_path)), "secret", "set", "telegram_token"]) == 1
    assert f"保存失败：密钥文件 {f} 别的用户也能访问" in capsys.readouterr().out


def test_extension_token_on_linux_is_kept_in_the_file(tmp_path, linux, capsys):
    from octopus.cli import main

    cfg = _config(tmp_path)
    assert main(["--config", str(cfg), "extension", "token"]) == 0
    token = capsys.readouterr().out.splitlines()[1]
    assert f'"extension_token" = "{token}"' in (tmp_path / "data" / "secrets.toml").read_text(encoding="utf-8")
    assert main(["--config", str(cfg), "extension", "token"]) == 0
    assert token in capsys.readouterr().out  # made once


def test_channels_read_their_tokens_from_the_environment(tmp_path, linux, monkeypatch):
    from octopus.bootstrap import build_channels
    from octopus.config import load_config
    from octopus.secrets import Secrets

    monkeypatch.setenv("OCTOPUS_EXTENSION_TOKEN", "t" * 43)
    cfg = load_config(_config(tmp_path, "[channels.extension]\nenabled = true\n"))
    channels, problems = build_channels(cfg, Secrets(file=cfg.data_dir / "secrets.toml"), notify=None)
    assert problems == [] and channels["extension"].token == "t" * 43


def test_secret_set_on_a_mac_still_says_keychain(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod
    from octopus.cli import main
    from octopus.secrets import Secrets

    class Memory:
        store = {}

        def set_password(self, service, name, value):
            self.store[name] = value

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: Secrets(Memory(), **kw))
    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt: "123:abc")
    assert main(["--config", str(_config(tmp_path)), "secret", "set", "telegram_token"]) == 0
    assert capsys.readouterr().out.strip() == "已保存到钥匙串：telegram_token"
    assert not (tmp_path / "data" / "secrets.toml").exists()


# ---- ClawBot's QR code: `open` is macOS-only ----

def test_qr_opens_with_open_on_a_mac(tmp_path):
    from octopus.channels.clawbot import _open_file

    calls = []
    _open_file(tmp_path / "qr.png", run=lambda args, **kw: calls.append(args), environ={})
    assert calls == [["open", str(tmp_path / "qr.png")]]


def test_qr_opens_with_xdg_open_on_a_linux_desktop(tmp_path, linux, monkeypatch):
    import octopus.channels.clawbot as clawbot

    monkeypatch.setattr(clawbot.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []
    clawbot._open_file(tmp_path / "qr.png", run=lambda args, **kw: calls.append(args), environ={"DISPLAY": ":0"})
    assert calls == [["xdg-open", str(tmp_path / "qr.png")]]
    calls.clear()
    clawbot._open_file(tmp_path / "qr.png", run=lambda args, **kw: calls.append(args), environ={})  # a server
    assert calls == []
    monkeypatch.setattr(clawbot.shutil, "which", lambda name: None)
    clawbot._open_file(tmp_path / "qr.png", run=lambda args, **kw: calls.append(args), environ={"DISPLAY": ":0"})
    assert calls == []


def test_secret_list_says_where_each_value_comes_from_never_the_value(tmp_path, linux, monkeypatch, capsys):
    from octopus.cli import main

    monkeypatch.setattr("octopus.host.in_container", lambda: False)
    monkeypatch.setenv("OCTOPUS_FEISHU_APP_ID", "cli_secret_id")
    assert main(["--config", str(_config(tmp_path)), "secret", "list"]) == 0
    out = capsys.readouterr().out
    assert "feishu_app_id：已设置（环境变量 OCTOPUS_FEISHU_APP_ID）" in out and "cli_secret_id" not in out
    assert "systemd" in out  # the service does not see a terminal's environment
    assert "telegram_token：未设置" in out


def test_in_a_container_the_environment_is_how_secrets_arrive(tmp_path, linux, monkeypatch, capsys):
    from octopus.cli import main

    monkeypatch.setattr("octopus.host.in_container", lambda: True)
    monkeypatch.setenv("OCTOPUS_FEISHU_APP_ID", "cli_secret_id")
    assert main(["--config", str(_config(tmp_path)), "secret", "list"]) == 0
    assert "systemd" not in capsys.readouterr().out


def test_secret_set_of_a_managed_secret_gives_no_environment_note(tmp_path, linux, monkeypatch, capsys):
    import octopus.cli as cli_mod
    from octopus.cli import main

    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt: "tok")
    monkeypatch.setenv("OCTOPUS_CLAWBOT_TOKEN", "stale")  # ignored for a managed secret: nothing to warn about
    assert main(["--config", str(_config(tmp_path)), "secret", "set", "clawbot_token"]) == 0
    assert "优先" not in capsys.readouterr().out


def test_models_on_linux_says_the_service_cannot_see_terminal_keys(tmp_path, linux, monkeypatch, capsys):
    from octopus.cli import main

    monkeypatch.setattr("octopus.host.in_container", lambda: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-term")
    cfg = _config(tmp_path, '[models.summarize]\nprimary = "anthropic:haiku"\n')
    assert main(["--config", str(cfg), "models"]) == 0
    out = capsys.readouterr().out
    assert "anthropic：已设置（环境变量 ANTHROPIC_API_KEY）" in out and "systemd" in out and "sk-term" not in out


def test_on_linux_secret_commands_need_a_config_that_loads(tmp_path, linux, capsys):
    from octopus.cli import main

    bad = tmp_path / "config.toml"
    bad.write_text("data_dir = \n", encoding="utf-8")
    assert main(["--config", str(bad), "secret", "list"]) == 2  # the file lives in its data_dir
    assert "配置文件格式有误" in capsys.readouterr().out


def test_on_a_mac_secret_commands_work_with_a_broken_config(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod
    from octopus.cli import main

    made = []

    class Keychain:
        def __init__(self, **kw):
            made.append(kw)
            self.store = {}

        def set(self, name, value):
            self.store[name] = value
            return "钥匙串"

        def get(self, name):
            return None

    monkeypatch.setattr(cli_mod, "Secrets", Keychain)
    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt: "123:abc")
    bad = tmp_path / "config.toml"
    bad.write_text("data_dir = \n", encoding="utf-8")
    assert main(["--config", str(bad), "secret", "set", "telegram_token"]) == 0
    assert capsys.readouterr().out.strip() == "已保存到钥匙串：telegram_token"
    assert made == [{}]  # the Keychain alone, as before P22: no data_dir needed
    assert main(["--config", str(bad), "extension", "token"]) == 0
    out = capsys.readouterr().out
    assert "Chrome 插件的密码" in out and "配置文件格式有误" in out  # the token, then what is wrong with the config


def test_on_a_mac_the_file_backend_still_needs_the_config(tmp_path, monkeypatch, capsys):
    import stat

    import octopus.cli as cli_mod
    from octopus.cli import main

    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt: "123:abc")
    cfg = _config(tmp_path, '[secrets]\nbackend = "file"\n')
    assert main(["--config", str(cfg), "secret", "set", "telegram_token"]) == 0
    f = tmp_path / "data" / "secrets.toml"
    assert f"已保存到密钥文件 {f}" in capsys.readouterr().out and stat.S_IMODE(f.stat().st_mode) == 0o600


# ---- M9: a backend named in the config but not installed ----

@pytest.mark.parametrize("key,value,package", [("asr_backend", "mlx", "mlx_whisper"),
                                               ("asr_backend", "faster-whisper", "faster_whisper"),
                                               ("ocr_backend", "ocrmac", "ocrmac"),
                                               ("ocr_backend", "rapidocr", "rapidocr")])
def test_a_backend_that_is_not_installed_is_said_at_startup(tmp_path, monkeypatch, capsys, key, value, package):
    import importlib.util

    from octopus.cli import main

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == package else real(name, *a))
    cfg = _config(tmp_path, f'[media]\n{key} = "{value}"\n')
    assert main(["--config", str(cfg), "models"]) == 2
    out = capsys.readouterr().out
    assert f"media.{key}" in out and "没有安装" in out


def test_auto_backends_are_never_checked_at_startup(tmp_path, monkeypatch, capsys):
    import importlib.util

    from octopus.cli import main

    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: pytest.fail(f"checked {name}"))
    assert main(["--config", str(_config(tmp_path)), "models"]) == 0
