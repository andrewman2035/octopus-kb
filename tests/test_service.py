import os
import plistlib
import stat
from pathlib import Path

import pytest

from octopus.cli import main
from octopus.service import (LABEL, START_CHECKING, START_SETTLE_CHECKS, START_SETTLE_EVERY, START_STILL_LOADED,
                             install, plist_dict, plist_path, start, status, stop, uninstall)


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def recorder(result=None):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return result or Result()

    run.calls = calls
    return run


def test_plist_contents(tmp_path):
    d = plist_dict("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", Path("/Users/me"))
    assert d["Label"] == LABEL
    assert d["ProgramArguments"] == ["/venv/bin/python", "-P", "-m", "octopus.cli", "--config", str(tmp_path / "config.toml"), "run"]
    assert d["KeepAlive"] is True and d["RunAtLoad"] is True and d["ThrottleInterval"] == 30
    assert d["StandardOutPath"].endswith("logs/launchd.out.log")
    assert d["StandardErrorPath"].endswith("logs/launchd.err.log")
    path = d["EnvironmentVariables"]["PATH"].split(":")
    assert "/opt/homebrew/bin" in path and "/usr/local/bin" in path and "/Users/me/.local/bin" in path
    assert d["WorkingDirectory"] == str(tmp_path / "Octopus" / "llm-workdir")
    assert d["EnvironmentVariables"]["PYTHONUNBUFFERED"] == "1"  # log lines reach launchd.out.log immediately


def test_install_writes_plist_and_bootstraps(tmp_path):
    run = recorder()
    ok, msg = install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path, run=run)
    assert ok is True
    p = plist_path(tmp_path)
    assert p == tmp_path / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert plistlib.loads(p.read_bytes())["Label"] == LABEL
    assert run.calls[-1][:2] == ["launchctl", "bootstrap"] and run.calls[-1][-1] == str(p)
    assert run.calls[-1][2] == f"gui/{os.getuid()}"
    assert "已安装" in msg
    uninstall(home=tmp_path, run=run)
    assert run.calls[-1][:2] == ["launchctl", "bootout"] and not p.exists()


def test_install_plist_is_world_readable_but_logs_stay_private(tmp_path):
    old = os.umask(0o077)  # a strict umask must not make launchd reject the plist
    try:
        install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path, run=recorder())
    finally:
        os.umask(old)
    assert stat.S_IMODE(plist_path(tmp_path).stat().st_mode) == 0o644
    assert stat.S_IMODE((tmp_path / "Octopus" / "logs").stat().st_mode) == 0o700


SVC_TARGET = f"gui/{os.getuid()}/{LABEL}"


def test_install_always_boots_out_by_label_first_even_without_a_plist(tmp_path):
    run = recorder()
    install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path, run=run)
    assert run.calls[0] == ["launchctl", "bootout", SVC_TARGET]
    assert [c[1] for c in run.calls] == ["bootout", "bootstrap"]
    run.calls.clear()
    install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path, run=run)
    assert [c[1] for c in run.calls] == ["bootout", "bootstrap"]


def test_install_ignores_bootout_failure(tmp_path):
    def run(args, **kw):
        return Result(113, stderr="not loaded") if args[1] == "bootout" else Result()

    ok, _ = install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path, run=run)
    assert ok is True


@pytest.mark.parametrize("rc", [5, 37])
def test_install_retries_bootstrap_after_a_pause(tmp_path, rc):
    attempts, sleeps = [], []

    def run(args, **kw):
        if args[1] == "bootstrap":
            attempts.append(1)
            return Result(rc, stderr="Bootstrap failed") if len(attempts) < 4 else Result()
        return Result()

    ok, msg = install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path,
                      run=run, sleep=sleeps.append)
    assert ok is True and len(attempts) == 4 and sleeps == [2, 2, 2]
    assert "已安装" in msg


def test_install_reports_bootstrap_failure_after_about_twenty_seconds(tmp_path):
    attempts, sleeps = [], []

    def run(args, **kw):
        if args[1] == "bootstrap":
            attempts.append(1)
            return Result(5, stderr="Bootstrap failed: 5")
        return Result()

    ok, msg = install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path,
                      run=run, sleep=sleeps.append)
    assert ok is False and len(attempts) == 11 and sleeps == [2] * 10
    assert "启动失败：Bootstrap failed: 5" in msg
    assert "（如果服务已经在运行，先运行 octopus service uninstall）" in msg
    assert plist_path(tmp_path).exists()


def test_install_does_not_retry_other_errors(tmp_path):
    attempts = []

    def run(args, **kw):
        if args[1] == "bootstrap":
            attempts.append(1)
            return Result(1, stderr="nope")
        return Result()

    ok, msg = install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path,
                      run=run, sleep=lambda s: pytest.fail("slept"))
    assert ok is False and len(attempts) == 1 and "启动失败：nope" in msg


def test_missing_tools_lists_what_is_not_on_the_service_path(tmp_path):
    from octopus.service import missing_tools

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "ffmpeg"):
        f = bin_dir / name
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    assert missing_tools("claude", str(bin_dir)) == []
    assert missing_tools("claude", str(tmp_path / "empty")) == ["claude", "ffmpeg"]
    assert missing_tools(str(bin_dir / "claude"), str(tmp_path / "empty")) == ["ffmpeg"]  # an absolute path needs no PATH
    assert missing_tools("/nope/claude", str(bin_dir)) == ["/nope/claude"]


def test_install_warns_when_claude_or_ffmpeg_is_not_found(tmp_path, monkeypatch):
    import octopus.service as svc

    monkeypatch.setattr(svc.shutil, "which", lambda name, path=None: None)
    ok, msg = install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path,
                      run=recorder(), claude_bin="claude")
    assert ok is True and "已安装" in msg
    assert "警告" in msg and "claude" in msg and "ffmpeg" in msg


def test_install_has_no_warning_when_tools_are_found(tmp_path, monkeypatch):
    import octopus.service as svc

    monkeypatch.setattr(svc.shutil, "which", lambda name, path=None: f"/x/{name}")
    ok, msg = install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path, run=recorder())
    assert ok is True and "警告" not in msg


def test_cli_service_install_passes_claude_bin(tmp_path, monkeypatch):
    import octopus.service as svc

    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path / "Octopus"}"\nclaude_bin = "/opt/claude"\n', encoding="utf-8")
    seen = {}
    monkeypatch.setattr(svc, "install", lambda python, config_path, data_dir, **kw: seen.update(kw) or (True, "ok"))
    assert main(["--config", str(cfg), "service", "install"]) == 0
    assert seen["claude_bin"] == "/opt/claude"


def test_install_without_launchctl_fails(tmp_path):
    def run(args, **kw):
        raise FileNotFoundError("launchctl")

    ok, msg = install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path, run=run)
    assert ok is False and "launchctl" in msg


def test_uninstall_boots_out_by_label_and_removes_plist(tmp_path):
    run = recorder()
    p = plist_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    assert uninstall(home=tmp_path, run=run) == "已卸载后台服务。"
    assert run.calls == [["launchctl", "bootout", SVC_TARGET]]
    assert not p.exists()


def test_uninstall_works_without_the_plist_file(tmp_path):
    assert uninstall(home=tmp_path, run=recorder()) == "已卸载后台服务。"  # loaded job, file already gone


def test_uninstall_when_nothing_is_installed(tmp_path):
    run = recorder(Result(113, stderr="Could not find service"))
    assert uninstall(home=tmp_path, run=run) == "后台服务没有在运行。"


def test_uninstall_not_loaded_but_plist_left_behind(tmp_path):
    p = plist_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    assert uninstall(home=tmp_path, run=recorder(Result(113))) == "已卸载后台服务。"
    assert not p.exists()


def test_status_reports_not_loaded():
    run = lambda args, **kw: Result(113, stderr="Could not find service")
    assert "没有在运行" in status(run=run)


def test_status_reports_state_and_pid(tmp_path):
    out = "com.octopus.agent = {\n\tactive count = 1\n\tstate = running\n\n\tpid = 4242\n\tlast exit code = (never exited)\n}\n"
    run = recorder(Result(0, stdout=out))
    msg = status(home=tmp_path, run=run)
    assert "state = running" in msg and "pid = 4242" in msg
    assert run.calls[0] == ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"]


def test_status_reports_last_exit_code_and_log_paths(tmp_path):
    out = ("com.octopus.agent = {\n\tstate = waiting\n\n\tlast exit code = 1\n\truns = 3\n"
           "\tproperties = {\n\t\tsuccessful exit => 0\n\t}\n}\n")
    data = tmp_path / "Octopus"
    install("/venv/bin/python", tmp_path / "config.toml", data, home=tmp_path, run=recorder())
    msg = status(home=tmp_path, run=recorder(Result(0, stdout=out)))
    assert "上次退出码：1" in msg
    assert str(data / "logs" / "launchd.out.log") in msg and str(data / "logs" / "launchd.err.log") in msg
    assert "successful" not in msg


def test_status_exit_code_never_exited_and_no_plist(tmp_path):
    out = "com.octopus.agent = {\n\tstate = running\n\tpid = 7\n\tlast exit code = (never exited)\n}\n"
    msg = status(home=tmp_path, run=recorder(Result(0, stdout=out)))
    assert "上次退出码：(never exited)" in msg and "pid = 7" in msg


def test_status_when_launchctl_is_missing():
    def run(args, **kw):
        raise FileNotFoundError("launchctl")

    assert "没有在运行" in status(run=run)


def _config(tmp_path):
    data = tmp_path / "Octopus"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{data}"\n', encoding="utf-8")
    return cfg, data


def test_cli_service_install_uses_absolute_python_and_config(tmp_path, monkeypatch, capsys):
    import octopus.service as svc

    cfg, data = _config(tmp_path)
    seen = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(svc, "install", lambda python, config_path, data_dir, **kw: seen.update(
        python=python, config=config_path, data=data_dir) or (True, "已安装后台服务：x"))
    assert main(["--config", "config.toml", "service", "install"]) == 0
    import sys
    assert seen["python"] == sys.executable
    assert seen["config"].is_absolute() and seen["config"] == cfg.resolve()
    assert seen["data"] == data
    assert (data / "logs").is_dir() and stat.S_IMODE(data.stat().st_mode) == 0o700  # ensure_dirs ran
    out = capsys.readouterr().out
    assert "已安装后台服务" in out and "钥匙串" in out


def test_cli_service_install_creates_missing_config(tmp_path, monkeypatch, capsys):
    import octopus.service as svc

    cfg = tmp_path / "sub" / "config.toml"
    monkeypatch.setattr(svc, "install", lambda *a, **k: (True, "已安装后台服务：x"))
    # the default template points at ~/Octopus: keep ensure_dirs away from the real one
    monkeypatch.setattr("octopus.cli.ensure_dirs", lambda c: None)
    assert main(["--config", str(cfg), "service", "install"]) == 0
    assert cfg.exists()


def test_cli_service_install_failure_exits_1(tmp_path, monkeypatch, capsys):
    import octopus.service as svc

    cfg, _ = _config(tmp_path)
    monkeypatch.setattr(svc, "install", lambda *a, **k: (False, "写入了 x，但启动失败：boom"))
    assert main(["--config", str(cfg), "service", "install"]) == 1
    assert "启动失败：boom" in capsys.readouterr().out


def test_cli_service_install_renders_an_absolute_data_dir(tmp_path, monkeypatch):
    import octopus.service as svc

    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text('data_dir = "rel-data"\n', encoding="utf-8")
    seen = {}
    monkeypatch.setattr(svc, "install", lambda python, config_path, data_dir, **kw: seen.update(data=data_dir) or (True, "ok"))
    assert main(["--config", str(cfg), "service", "install"]) == 0
    assert seen["data"].is_absolute() and seen["data"] == (tmp_path / "rel-data").resolve()


def test_cli_service_install_bad_config_exits_2(tmp_path, capsys):
    bad = tmp_path / "config.toml"
    bad.write_text("data_dir = \n", encoding="utf-8")
    assert main(["--config", str(bad), "service", "install"]) == 2


@pytest.mark.parametrize("sub", ["uninstall", "status"])
def test_cli_service_uninstall_and_status(tmp_path, monkeypatch, capsys, sub):
    import octopus.service as svc

    monkeypatch.setattr(svc, sub, lambda **kw: f"{sub} ok")
    assert main(["--config", str(tmp_path / "nope.toml"), "service", sub]) == 0
    assert f"{sub} ok" in capsys.readouterr().out


def test_cli_service_restart_installs_again(tmp_path, monkeypatch, capsys):
    import octopus.service as svc

    cfg, data = _config(tmp_path)
    seen = []
    monkeypatch.setattr(svc, "install", lambda python, config_path, data_dir, **kw: seen.append((config_path, data_dir)) or (True, "已安装后台服务：x"))
    assert main(["--config", str(cfg), "service", "restart"]) == 0
    assert seen == [(cfg.resolve(), data)]
    assert "已安装后台服务" in capsys.readouterr().out


def _installed(tmp_path):
    p = plist_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    return p


def test_stop_boots_out_by_label_and_keeps_the_plist(tmp_path):
    p = _installed(tmp_path)
    run = recorder()
    assert stop(home=tmp_path, run=run) == (True, "已停止（后台还在收尾，几秒后再运行 chat / reindex）。下次登录会自动启动；想彻底关掉用 service uninstall。")
    assert run.calls == [["launchctl", "bootout", SVC_TARGET]]
    assert p.exists()


def test_stop_when_installed_but_not_loaded_is_idempotent(tmp_path):
    _installed(tmp_path)
    ok, msg = stop(home=tmp_path, run=recorder(Result(113, stderr="Could not find service")))
    assert ok is True and msg.startswith("已停止（后台还在收尾")


def test_stop_when_not_installed(tmp_path):
    run = recorder()
    assert stop(home=tmp_path, run=run) == (False, "服务没有安装。")
    assert run.calls == []


def test_stop_without_launchctl_fails(tmp_path):
    _installed(tmp_path)

    def run(args, **kw):
        raise FileNotFoundError("launchctl")

    ok, msg = stop(home=tmp_path, run=run)
    assert ok is False and "launchctl" in msg


def test_start_bootstraps_the_installed_plist(tmp_path):
    p = _installed(tmp_path)
    calls = []

    def run(args, **kw):
        calls.append(args)
        return Result(113) if args[1] == "print" else Result()

    said = []
    ok, msg = start(home=tmp_path, run=run, sleep=lambda s: None, say=said.append)
    assert ok is True and msg == "已启动后台服务。" and said == []  # not loaded: nothing to wait for
    assert calls == [["launchctl", "print", SVC_TARGET], ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(p)]]


def test_start_when_already_running(tmp_path):
    _installed(tmp_path)
    run, sleeps, said = recorder(Result(0, stdout="state = running")), [], []
    assert start(home=tmp_path, run=run, sleep=sleeps.append, say=said.append) == (True, START_STILL_LOADED)
    assert said == [START_CHECKING]  # once, before the wait: it is not silent
    assert [c[1] for c in run.calls] == ["print"] * (START_SETTLE_CHECKS + 1)  # never bootstrapped over it
    assert sleeps == [START_SETTLE_EVERY] * START_SETTLE_CHECKS


def test_start_right_after_stop_waits_for_the_shutdown_then_starts(tmp_path):
    """`service stop` then `service start` at once: the old job is still being torn down (launchctl print still
    finds it). start waits for it to go, then starts; it never says it is running."""
    p = _installed(tmp_path)
    calls, sleeps = [], []

    def run(args, **kw):
        calls.append(args)
        if args[1] == "print":
            return Result(0, stdout="state = running") if len(calls) <= 2 else Result(113)
        return Result()

    said = []
    ok, msg = start(home=tmp_path, run=run, sleep=sleeps.append, say=said.append)
    assert (ok, msg) == (True, "已启动后台服务。") and said == [START_CHECKING]
    assert calls == [["launchctl", "print", SVC_TARGET]] * 3 + [
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(p)]]
    assert sleeps == [START_SETTLE_EVERY] * 2


def test_start_message_when_still_loaded_does_not_only_claim_running():
    assert "收尾" in START_STILL_LOADED and "octopus service start" in START_STILL_LOADED


def test_start_when_not_installed(tmp_path):
    run = recorder()
    assert start(home=tmp_path, run=run) == (False, "服务没有安装，先运行 octopus service install。")
    assert run.calls == []


@pytest.mark.parametrize("rc", [5, 37])
def test_start_retries_bootstrap_after_a_pause(tmp_path, rc):
    _installed(tmp_path)
    attempts, sleeps = [], []

    def run(args, **kw):
        if args[1] == "print":
            return Result(113)
        attempts.append(1)
        return Result(rc, stderr="Bootstrap failed") if len(attempts) < 3 else Result()

    ok, _ = start(home=tmp_path, run=run, sleep=sleeps.append)
    assert ok is True and len(attempts) == 3 and sleeps == [2, 2]


def test_start_reports_bootstrap_failure(tmp_path):
    _installed(tmp_path)
    sleeps = []

    def run(args, **kw):
        return Result(113) if args[1] == "print" else Result(5, stderr="Bootstrap failed: 5")

    ok, msg = start(home=tmp_path, run=run, sleep=sleeps.append)
    assert ok is False and sleeps == [2] * 10 and msg == "启动失败：Bootstrap failed: 5"


def test_start_does_not_retry_other_errors(tmp_path):
    _installed(tmp_path)
    attempts = []

    def run(args, **kw):
        if args[1] == "print":
            return Result(113)
        attempts.append(1)
        return Result(1, stderr="bad plist")

    ok, msg = start(home=tmp_path, run=run, sleep=lambda s: pytest.fail("no retry"))
    assert ok is False and len(attempts) == 1 and "bad plist" in msg


def test_start_without_launchctl_fails(tmp_path):
    _installed(tmp_path)

    def run(args, **kw):
        raise FileNotFoundError("launchctl")

    ok, msg = start(home=tmp_path, run=run)
    assert ok is False and "launchctl" in msg


@pytest.mark.parametrize("sub", ["stop", "start"])
@pytest.mark.parametrize("ok, code", [(True, 0), (False, 1)])
def test_cli_service_stop_and_start_exit_codes(tmp_path, monkeypatch, capsys, sub, ok, code):
    import octopus.service as svc

    monkeypatch.setattr(svc, sub, lambda **kw: (ok, f"{sub} message"))
    assert main(["--config", str(tmp_path / "nope.toml"), "service", sub]) == code
    assert f"{sub} message" in capsys.readouterr().out


# ---- P28: the plist's LANG follows the config's language ----

def test_the_chinese_plist_is_exactly_as_before(tmp_path):
    home, data, config = Path("/Users/me"), tmp_path / "Octopus", tmp_path / "config.toml"
    before = {
        "Label": "com.octopus.agent",
        "ProgramArguments": ["/venv/bin/python", "-P", "-m", "octopus.cli", "--config", str(config), "run"],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "WorkingDirectory": str(data / "llm-workdir"),
        "StandardOutPath": str(data / "logs" / "launchd.out.log"),
        "StandardErrorPath": str(data / "logs" / "launchd.err.log"),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/Users/me/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": "/Users/me", "LANG": "zh_CN.UTF-8", "PYTHONUNBUFFERED": "1"},
    }
    assert plist_dict("/venv/bin/python", config, data, home) == before
    assert plist_dict("/venv/bin/python", config, data, home, "zh") == before
    assert plistlib.dumps(plist_dict("/venv/bin/python", config, data, home, "zh")) == plistlib.dumps(before)


def test_an_english_config_gets_an_english_utf8_locale(tmp_path):
    d = plist_dict("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", Path("/Users/me"), "en")
    assert d["EnvironmentVariables"]["LANG"] == "en_US.UTF-8"


def test_install_writes_the_plist_in_the_configs_language(tmp_path):
    install("/venv/bin/python", tmp_path / "config.toml", tmp_path / "Octopus", home=tmp_path, run=recorder(),
            language="en")
    assert plistlib.loads(plist_path(tmp_path).read_bytes())["EnvironmentVariables"]["LANG"] == "en_US.UTF-8"


@pytest.mark.parametrize("line, language", [("", "zh"), ('language = "zh"\n', "zh"), ('language = "en"\n', "en")])
def test_cli_service_install_passes_the_configs_language(tmp_path, monkeypatch, line, language):
    import octopus.service as svc

    cfg = tmp_path / "config.toml"
    cfg.write_text(f'{line}data_dir = "{tmp_path / "Octopus"}"\n', encoding="utf-8")
    seen = {}
    monkeypatch.setattr(svc, "install", lambda python, config_path, data_dir, **kw: seen.update(kw) or (True, "ok"))
    assert main(["--config", str(cfg), "service", "install"]) == 0
    assert seen["language"] == language
