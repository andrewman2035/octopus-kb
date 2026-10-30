"""P22: `octopus service` on Linux is a systemd --user unit. Every systemctl / loginctl call goes to a fake runner."""
import stat
from pathlib import Path

import pytest

from octopus import host, systemd
from octopus.cli import main
from octopus.systemd import UNIT, install, start, status, stop, uninstall, unit_path, unit_text


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def recorder(answers=None):
    """A fake subprocess.run: answers maps a systemctl/loginctl verb to a Result."""
    calls = []

    def run(args, **kw):
        calls.append(args)
        verb = args[2] if args[0] == "systemctl" else args[1]
        return (answers or {}).get(verb, Result())

    run.calls = calls
    return run


def test_unit_runs_octopus_like_the_launchd_job(tmp_path):
    text = unit_text("/venv/bin/python", Path("/srv/oct/config.toml"), Path("/srv/oct"), Path("/home/me"))
    assert 'ExecStart="/venv/bin/python" "-P" "-m" "octopus.cli" "--config" "/srv/oct/config.toml" "run"' in text
    assert "Restart=on-failure\n" in text and "RestartSec=30\n" in text
    assert "WorkingDirectory=/srv/oct/llm-workdir\n" in text
    assert "StandardOutput=append:/srv/oct/logs/systemd.out.log\n" in text
    assert "StandardError=append:/srv/oct/logs/systemd.err.log\n" in text
    assert '"HOME=/home/me"' in text and '"PYTHONUNBUFFERED=1"' in text and '"LANG=C.UTF-8"' in text
    path = text.split('"PATH=', 1)[1].split('"', 1)[0].split(":")
    assert "/home/me/.local/bin" in path and "/usr/bin" in path and "/opt/homebrew/bin" not in path
    assert "WantedBy=default.target" in text


def test_unit_quotes_spaces_and_escapes_what_systemd_would_expand():
    text = unit_text('/my venv/py"thon', Path("/a b/100%/$HOME/config.toml"), Path("/d"), Path("/h$x"))
    assert r'ExecStart="/my venv/py\"thon" "-P"' in text
    assert '"/a b/100%%/$$HOME/config.toml"' in text
    assert '"HOME=/h$x"' in text  # Environment= expands no variables: a $ stays single


def test_install_writes_the_unit_enables_and_restarts(tmp_path):
    run = recorder({"show-user": Result(0, "yes\n")})
    ok, msg = install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path, run=run,
                      user="me")
    assert ok is True
    p = unit_path(tmp_path)
    assert p == tmp_path / ".config" / "systemd" / "user" / "octopus.service"
    assert stat.S_IMODE(p.stat().st_mode) == 0o644 and "ExecStart=" in p.read_text(encoding="utf-8")
    assert stat.S_IMODE((tmp_path / "Octopus" / "logs").stat().st_mode) == 0o700
    assert run.calls[:3] == [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", UNIT],
                             ["systemctl", "--user", "restart", UNIT]]
    assert "已安装后台服务" in msg and "linger" not in msg  # lingering already on


def test_install_says_how_to_run_without_a_login_session(tmp_path):
    run = recorder({"show-user": Result(0, "no\n")})
    ok, msg = install("/venv/bin/python", tmp_path / "c.toml", tmp_path / "O", home=tmp_path, run=run, user="me")
    assert ok is True and "loginctl enable-linger me" in msg
    assert run.calls[-1] == ["loginctl", "show-user", "me", "--property=Linger", "--value"]


def test_install_reports_a_failed_start(tmp_path):
    run = recorder({"restart": Result(1, stderr="Failed to connect to bus: No medium found")})
    ok, msg = install("/venv/bin/python", tmp_path / "c.toml", tmp_path / "O", home=tmp_path, run=run, user="me")
    assert ok is False and "No medium found" in msg and unit_path(tmp_path).exists()


def test_install_without_systemctl(tmp_path):
    def run(args, **kw):
        raise FileNotFoundError("systemctl")

    ok, msg = install("/venv/bin/python", tmp_path / "c.toml", tmp_path / "O", home=tmp_path, run=run, user="me")
    assert ok is False and "systemctl" in msg and "Docker" in msg


def test_install_warns_about_missing_tools(tmp_path, monkeypatch):
    monkeypatch.setattr("octopus.service.shutil.which", lambda name, path=None: None)  # missing_tools is shared
    ok, msg = install("/venv/bin/python", tmp_path / "c.toml", tmp_path / "O", home=tmp_path,
                      run=recorder({"show-user": Result(0, "yes")}), user="me", claude_bin="claude")
    assert ok is True and "警告" in msg and "ffmpeg" in msg


def test_uninstall_disables_and_removes_the_unit(tmp_path):
    install("/venv/bin/python", tmp_path / "c.toml", tmp_path / "O", home=tmp_path, run=recorder(), user="me")
    run = recorder()
    assert uninstall(home=tmp_path, run=run) == "已卸载后台服务。"
    assert run.calls == [["systemctl", "--user", "disable", "--now", UNIT], ["systemctl", "--user", "daemon-reload"]]
    assert not unit_path(tmp_path).exists()


def test_uninstall_when_nothing_is_installed(tmp_path):
    assert uninstall(home=tmp_path, run=recorder({"disable": Result(1)})) == "后台服务没有在运行。"


def test_stop_and_start(tmp_path):
    assert stop(home=tmp_path, run=recorder()) == (False, "服务没有安装。")
    assert start(home=tmp_path, run=recorder()) == (False, "服务没有安装，先运行 octopus service install。")
    install("/venv/bin/python", tmp_path / "c.toml", tmp_path / "O", home=tmp_path, run=recorder(), user="me")
    run = recorder()
    ok, msg = stop(home=tmp_path, run=run)
    assert ok is True and msg.startswith("已停止") and run.calls == [["systemctl", "--user", "stop", UNIT]]
    assert unit_path(tmp_path).exists()
    run = recorder()
    assert start(home=tmp_path, run=run) == (True, "已启动后台服务。")
    assert run.calls == [["systemctl", "--user", "start", UNIT]]
    assert start(home=tmp_path, run=recorder({"start": Result(1, stderr="boom")})) == (False, "启动失败：boom")


def test_status_running_with_log_paths(tmp_path):
    data = tmp_path / "O"
    install("/venv/bin/python", tmp_path / "c.toml", data, home=tmp_path, run=recorder(), user="me")
    out = "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=4242\nExecMainStatus=0\n"
    run = recorder({"show": Result(0, out)})
    msg = status(home=tmp_path, run=run)
    assert "active（running）" in msg and "pid = 4242" in msg and "上次退出码" not in msg
    assert str(data / "logs" / "systemd.out.log") in msg and str(data / "logs" / "systemd.err.log") in msg
    assert run.calls[0][:4] == ["systemctl", "--user", "show", UNIT]


def test_status_failed_shows_the_exit_code():
    out = "LoadState=loaded\nActiveState=failed\nSubState=failed\nMainPID=0\nExecMainStatus=1\n"
    msg = status(home=Path("/nowhere"), run=recorder({"show": Result(0, out)}))
    assert "failed" in msg and "上次退出码：1" in msg and "pid" not in msg


@pytest.mark.parametrize("answer", [Result(0, "LoadState=not-found\nActiveState=inactive\n"), Result(1)])
def test_status_not_installed(answer):
    assert status(home=Path("/nowhere"), run=recorder({"show": answer})) == "后台服务没有在运行。"


def test_status_without_systemctl():
    def run(args, **kw):
        raise FileNotFoundError("systemctl")

    assert status(home=Path("/nowhere"), run=run) == "后台服务没有在运行。"


# ---- the CLI picks launchd or systemd ----

def _config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path / "Octopus"}"\n', encoding="utf-8")
    return cfg


def test_cli_uses_systemd_on_linux(tmp_path, monkeypatch, capsys):
    import octopus.service as launchd

    monkeypatch.setattr(host, "system", lambda: host.LINUX)
    seen = []
    monkeypatch.setattr(systemd, "install", lambda python, config_path, data_dir, **kw: seen.append(
        (config_path, data_dir, kw)) or (True, "已安装后台服务：octopus.service"))
    monkeypatch.setattr(systemd, "status", lambda **kw: "systemd status")
    monkeypatch.setattr(systemd, "stop", lambda **kw: (True, "systemd stop"))
    monkeypatch.setattr(launchd, "install", lambda *a, **k: pytest.fail("launchd on Linux"))
    cfg = _config(tmp_path)
    assert main(["--config", str(cfg), "service", "install"]) == 0
    assert seen[0][0] == cfg.resolve() and seen[0][2]["claude_bin"] == "claude"
    out = capsys.readouterr().out
    assert "已安装后台服务" in out and "secrets.toml" in out and "钥匙串密钥" not in out
    assert main(["--config", str(cfg), "service", "status"]) == 0
    assert "systemd status" in capsys.readouterr().out
    assert main(["--config", str(cfg), "service", "stop"]) == 0
    assert "systemd stop" in capsys.readouterr().out


def test_cli_service_on_another_system_says_it_is_not_available(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(host, "system", lambda: host.OTHER)
    assert main(["--config", str(_config(tmp_path)), "service", "install"]) == 1
    assert "macOS" in capsys.readouterr().out


def test_stop_reports_a_failed_systemctl(tmp_path):
    install("/venv/bin/python", tmp_path / "c.toml", tmp_path / "O", home=tmp_path, run=recorder(), user="me")
    ok, msg = stop(home=tmp_path, run=recorder({"stop": Result(1, stderr="Failed to connect to bus")}))
    assert ok is False and msg == "停止失败：Failed to connect to bus"


def test_uninstall_says_when_systemd_cannot_be_reached(tmp_path):
    install("/venv/bin/python", tmp_path / "c.toml", tmp_path / "O", home=tmp_path, run=recorder(), user="me")
    msg = uninstall(home=tmp_path, run=recorder({"disable": Result(1, stderr="Failed to connect to bus")}))
    assert "Failed to connect to bus" in msg and not unit_path(tmp_path).exists()


def test_the_unit_keeps_the_neutral_utf8_locale_whatever_the_language(tmp_path):
    # C.UTF-8 is the one UTF-8 locale every Linux has, and it carries no language (P28)
    def run(args, **kw):
        return type("R", (), {"returncode": 0, "stdout": "yes\n", "stderr": ""})()

    ok, _ = systemd.install("/venv/bin/python", tmp_path / "c.toml", tmp_path / "data", home=tmp_path, run=run,
                            user="me", language="en")
    assert ok and '"LANG=C.UTF-8"' in systemd.unit_path(tmp_path).read_text(encoding="utf-8")
