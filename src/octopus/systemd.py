"""systemd --user unit that keeps `octopus run` alive on Linux (P22): the launchd job's counterpart (service.py).
Every systemctl / loginctl call goes through `run`, so tests use fakes."""
from __future__ import annotations

import getpass
import subprocess
from pathlib import Path

from octopus.i18n import aliases, t
from octopus.service import missing_tools

UNIT = "octopus.service"
RESTART_SEC = 30  # like launchd's ThrottleInterval: never restart faster than every 30 s
__getattr__ = aliases({"NOT_RUNNING": "service.not_running", "NOT_INSTALLED": "service.not_installed",
                       "NO_SYSTEMCTL": "systemd.no_systemctl", "LINGER_HINT": "systemd.linger_hint",
                       "UNINSTALL_HALF": "systemd.uninstall_half", "STOPPED": "systemd.stopped"})


def _no_systemctl(verb: str, error: Exception) -> str:
    return t("systemd.no_systemctl", what=t(f"systemd.verb.{verb}"), error=error)


def unit_path(home: Path) -> Path:
    return home / ".config" / "systemd" / "user" / UNIT


def service_path(home: Path) -> str:
    return ":".join(["/usr/local/bin", str(home / ".local" / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


def _quote(arg: str, variables: bool = True) -> str:
    """One ExecStart / Environment word: double-quoted, with systemd's specifier (%) expansion turned off, and its
    $VARIABLE expansion too where it has one (ExecStart, not Environment)."""
    arg = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{arg.replace("$", "$$") if variables else arg}"'


def unit_text(python: str, config_path: Path, data_dir: Path, home: Path) -> str:
    logs = data_dir / "logs"
    # -P: the working directory is not put on sys.path (only the installed octopus can be imported)
    exec_start = " ".join(_quote(a) for a in [python, "-P", "-m", "octopus.cli", "--config", str(config_path), "run"])
    env = " ".join(_quote(f"{k}={v}", variables=False) for k, v in (
        ("PATH", service_path(home)), ("HOME", str(home)),
        ("LANG", "C.UTF-8"),  # always present on Linux; zh_CN.UTF-8 often is not installed
        ("PYTHONUNBUFFERED", "1")))

    def path(p: Path) -> str:
        return str(p).replace("%", "%%")

    return (
        f"{t('systemd.unit_comment')}"
        "[Unit]\n"
        "Description=Octopus personal knowledge base\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={path(data_dir / 'llm-workdir')}\n"
        f"Environment={env}\n"
        # `octopus run` exits 1 on a lock conflict or a dead worker, 0 after a stop
        "Restart=on-failure\n"
        f"RestartSec={RESTART_SEC}\n"
        f"StandardOutput=append:{path(logs / 'systemd.out.log')}\n"
        f"StandardError=append:{path(logs / 'systemd.err.log')}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemctl(run, *args: str):
    return run(["systemctl", "--user", *args], capture_output=True, text=True)


def _lingering(run, user: str) -> bool:
    try:
        r = run(["loginctl", "show-user", user, "--property=Linger", "--value"], capture_output=True, text=True)
    except OSError:
        return False
    return r.returncode == 0 and r.stdout.strip() == "yes"


def install(python: str, config_path: Path, data_dir: Path, home: Path | None = None,
            run=subprocess.run, claude_bin: str = "claude", user: str | None = None, language: str = "zh"
            ) -> tuple[bool, str]:
    """(ok, message). ok is False when the unit is written but did not start. `restart` starts it, or restarts a
    running one with the new unit (service restart = install again, as on macOS). language (the config's, as for
    launchd) does not change the unit: its LANG is C.UTF-8, the one UTF-8 locale every Linux has."""
    home = home or Path.home()
    p = unit_path(home)
    for sub in ("logs", "llm-workdir"):  # systemd opens the log files itself, so the directories must exist
        (data_dir / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(unit_text(python, config_path, data_dir, home), encoding="utf-8")
        p.chmod(0o644)
        r = _systemctl(run, "daemon-reload")
        if r.returncode == 0:
            r = _systemctl(run, "enable", UNIT)
        if r.returncode == 0:
            r = _systemctl(run, "restart", UNIT)
    except OSError as e:
        return False, _no_systemctl("install", e)
    if r.returncode != 0:
        return False, t("systemd.start_failed_after_write", path=p, error=r.stderr.strip())
    msg = t("systemd.installed", unit=UNIT, logs=data_dir / "logs")
    if missing := missing_tools(claude_bin, service_path(home)):
        msg += t("service.missing_tools", tools=t("common.list_sep").join(missing))
    user = user or getpass.getuser()
    if not _lingering(run, user):
        msg += "\n" + t("systemd.linger_hint", user=user)
    return True, msg


def uninstall(home: Path | None = None, run=subprocess.run) -> str:
    p = unit_path(home or Path.home())
    failed = ""
    try:
        r = _systemctl(run, "disable", "--now", UNIT)
        loaded, failed = r.returncode == 0, r.stderr.strip() if r.returncode != 0 else ""
    except OSError:
        loaded = False  # no systemctl: nothing is running; still remove the file
    had_file = p.exists()
    p.unlink(missing_ok=True)
    if had_file:
        try:
            _systemctl(run, "daemon-reload")
        except OSError:
            pass
    if not loaded and not had_file:
        return t("service.not_running")
    if had_file and failed:  # an installed unit that systemctl could not disable (no user bus: su / sudo shells)
        return t("systemd.uninstall_half", path=p, error=failed)
    return t("service.uninstalled")


def stop(home: Path | None = None, run=subprocess.run) -> tuple[bool, str]:
    """Stop it but keep the unit enabled, so it starts again at the next boot or login. (ok, message)"""
    if not unit_path(home or Path.home()).exists():
        return False, t("service.not_installed")
    try:
        r = _systemctl(run, "stop", UNIT)  # waits for `octopus run` to finish its shutdown
    except OSError as e:
        return False, _no_systemctl("stop", e)
    if r.returncode != 0:
        return False, t("systemd.stop_failed", error=r.stderr.strip())
    return True, t("systemd.stopped")


def start(home: Path | None = None, run=subprocess.run) -> tuple[bool, str]:
    """systemctl stop waits for the shutdown, so unlike launchd there is nothing to wait for here."""
    if not unit_path(home or Path.home()).exists():
        return False, t("service.install_first")
    try:
        r = _systemctl(run, "start", UNIT)  # already running: nothing to do
    except OSError as e:
        return False, _no_systemctl("start", e)
    if r.returncode != 0:
        return False, t("service.start_failed", error=r.stderr.strip())
    return True, t("service.started")


def status(home: Path | None = None, run=subprocess.run) -> str:
    try:
        r = _systemctl(run, "show", UNIT, "--property=LoadState,ActiveState,SubState,MainPID,ExecMainStatus")
    except OSError:
        return t("service.not_running")
    props = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln) if r.returncode == 0 else {}
    if props.get("LoadState") != "loaded":
        return t("service.not_running")
    line = t("systemd.status", active=props.get("ActiveState", "?"), sub=props.get("SubState", "?"))
    if props.get("MainPID", "0") != "0":
        line += f" pid = {props['MainPID']}"
    out = [line]
    if (code := props.get("ExecMainStatus", "0")) != "0":
        out.append(t("service.last_exit", code=code))
    try:
        unit = unit_path(home or Path.home()).read_text(encoding="utf-8").splitlines()
        logs = [ln.split("append:", 1)[1].replace("%%", "%") for ln in unit if ln.startswith("Standard")]
        if logs:
            out.append(t("service.logs", logs=t("common.list_sep").join(logs)))
    except OSError:
        pass
    return "\n".join(out)
