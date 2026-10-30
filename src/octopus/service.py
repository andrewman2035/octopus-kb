"""LaunchAgent that keeps `octopus run` alive (starts at login, restarts after a crash)."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path

from octopus.i18n import aliases, t

LABEL = "com.octopus.agent"
BOOTSTRAP_RETRY = (5, 37)  # EIO / "operation already in progress": the previous job is still being torn down
BOOTSTRAP_RETRY_EVERY = 2  # seconds...
BOOTSTRAP_RETRIES = 10  # ...for about 20 s in all: bootout is asynchronous and a stopping Octopus drains its jobs
# right after `service stop` launchctl print still finds the job while it shuts down: start looks again this
# many times, this many seconds apart, before taking it as running
START_SETTLE_CHECKS = 5
START_SETTLE_EVERY = 2
__getattr__ = aliases({"START_CHECKING": "service.checking", "START_STILL_LOADED": "service.still_loaded"})


def plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def service_path(home: Path) -> str:
    return ":".join(["/opt/homebrew/bin", "/usr/local/bin", str(home / ".local" / "bin"),
                     "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


def missing_tools(claude_bin: str, path: str) -> list[str]:
    """Tools Octopus shells out to that launchd (which has only `path`) would not find."""
    return [tool for tool in (claude_bin, "ffmpeg") if shutil.which(tool, path=path) is None]


# A UTF-8 locale for the service and what it runs (claude, ffmpeg, yt-dlp): launchd gives a job none. Its language
# part follows the config's (P28); "zh" is exactly the plist from before.
SERVICE_LANG = {"zh": "zh_CN.UTF-8", "en": "en_US.UTF-8"}


def plist_dict(python: str, config_path: Path, data_dir: Path, home: Path, language: str = "zh") -> dict:
    logs = data_dir / "logs"
    return {
        "Label": LABEL,
        # -P: the working directory is not put on sys.path (only the installed octopus can be imported)
        "ProgramArguments": [python, "-P", "-m", "octopus.cli", "--config", str(config_path), "run"],
        "RunAtLoad": True,
        "KeepAlive": True,  # `octopus run` exits 1 on a lock conflict or a dead worker, so launchd restarts it
        "ThrottleInterval": 30,  # ...but never faster than every 30 s
        "WorkingDirectory": str(data_dir / "llm-workdir"),
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
        "EnvironmentVariables": {
            "PATH": service_path(home),
            "HOME": str(home),
            "LANG": SERVICE_LANG[language],
            "PYTHONUNBUFFERED": "1",
        },
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target() -> str:
    return f"{_domain()}/{LABEL}"


def _bootstrap(p: Path, run, sleep):
    """`launchctl bootstrap`, retried while the previous job is still going away (bootout is asynchronous)."""
    r = run(["launchctl", "bootstrap", _domain(), str(p)], capture_output=True, text=True)
    for _ in range(BOOTSTRAP_RETRIES):
        if r.returncode not in BOOTSTRAP_RETRY:
            break
        sleep(BOOTSTRAP_RETRY_EVERY)
        r = run(["launchctl", "bootstrap", _domain(), str(p)], capture_output=True, text=True)
    return r


def install(python: str, config_path: Path, data_dir: Path, home: Path | None = None,
            run=subprocess.run, sleep=time.sleep, claude_bin: str = "claude", language: str = "zh"
            ) -> tuple[bool, str]:
    """(ok, message). ok is False when the plist is written but the job did not start. language: the config's."""
    home = home or Path.home()
    p = plist_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    for sub in ("logs", "llm-workdir"):  # launchd opens the log files itself, so the directories must exist
        (data_dir / sub).mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        # Unload any old job by label (works whether or not its plist is still there); "not loaded" is fine.
        run(["launchctl", "bootout", _target()], capture_output=True, text=True)
        p.write_bytes(plistlib.dumps(plist_dict(python, config_path, data_dir, home, language)))
        p.chmod(0o644)  # launchd refuses plists that are group/world-writable; the umask must not matter
        r = _bootstrap(p, run, sleep)
    except OSError as e:
        return False, t("service.install_failed", path=p, error=e)
    if r.returncode != 0:
        return False, t("service.start_failed_after_write", path=p, error=r.stderr.strip())
    msg = t("service.installed", label=LABEL, logs=data_dir / "logs")
    if missing := missing_tools(claude_bin, service_path(home)):
        msg += t("service.missing_tools", tools=t("common.list_sep").join(missing))
    return True, msg


def uninstall(home: Path | None = None, run=subprocess.run) -> str:
    p = plist_path(home or Path.home())
    try:
        loaded = run(["launchctl", "bootout", _target()], capture_output=True, text=True).returncode == 0
    except OSError:
        loaded = False  # no launchctl: nothing is loaded; still remove the file
    had_file = p.exists()
    p.unlink(missing_ok=True)
    if not loaded and not had_file:
        return t("service.not_running")
    return t("service.uninstalled")


def stop(home: Path | None = None, run=subprocess.run) -> tuple[bool, str]:
    """Unload the job but keep the plist, so it starts again at the next login. (ok, message)."""
    if not plist_path(home or Path.home()).exists():
        return False, t("service.not_installed")
    try:
        run(["launchctl", "bootout", _target()], capture_output=True, text=True)  # "not loaded" is fine: already stopped
    except OSError as e:
        return False, t("service.stop_failed", error=e)
    return True, t("service.stopped")


def start(home: Path | None = None, run=subprocess.run, sleep=time.sleep, say=print) -> tuple[bool, str]:
    """Load the installed plist. (ok, message). A job still shutting down after `service stop` is waited for,
    with say(START_CHECKING) before the wait."""
    p = plist_path(home or Path.home())
    if not p.exists():
        return False, t("service.install_first")
    try:
        for check in range(START_SETTLE_CHECKS + 1):
            if run(["launchctl", "print", _target()], capture_output=True, text=True).returncode != 0:
                break
            if check == START_SETTLE_CHECKS:  # running, or a shutdown that takes longer: say both
                return True, t("service.still_loaded")
            if check == 0:
                say(t("service.checking"))
            sleep(START_SETTLE_EVERY)
        r = _bootstrap(p, run, sleep)
    except OSError as e:
        return False, t("service.launch_failed", error=e)
    if r.returncode != 0:
        return False, t("service.start_failed", error=r.stderr.strip())
    return True, t("service.started")


def status(home: Path | None = None, run=subprocess.run) -> str:
    try:
        r = run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    except OSError:
        return t("service.not_running")
    if r.returncode != 0:
        return t("service.not_running")
    lines = [ln.strip() for ln in r.stdout.splitlines()]
    state = next((ln for ln in lines if ln.startswith("state =")), "state = ?")
    pid = next((ln for ln in lines if ln.startswith("pid =")), "")
    out = [t("service.status", state=f"{state} {pid}".strip())]
    if code := next((ln.split("=", 1)[1].strip() for ln in lines if ln.startswith("last exit code =")), ""):
        out.append(t("service.last_exit", code=code))
    try:
        conf = plistlib.loads(plist_path(home or Path.home()).read_bytes())
        out.append(t("service.logs", logs=t("common.list_sep").join([conf["StandardOutPath"], conf["StandardErrorPath"]])))
    except (OSError, ValueError, KeyError):
        pass
    return "\n".join(out)
