"""Which operating system Octopus runs on (P22): macOS keeps its Apple backends (MLX, Vision, Keychain, launchd);
Linux gets portable ones (faster-whisper, RapidOCR, a secrets file, systemd). Read at call time, so tests can pin it."""
from __future__ import annotations

import os
import sys

MAC = "mac"
LINUX = "linux"
OTHER = "other"


def system_of(platform: str) -> str:
    if platform == "darwin":
        return MAC
    return LINUX if platform.startswith("linux") else OTHER


def system() -> str:
    return system_of(sys.platform)


def in_container(exists=os.path.exists, environ=None) -> bool:
    """Docker (/.dockerenv), Podman (/run/.containerenv) or anything that sets $container (systemd-nspawn, Podman)."""
    environ = os.environ if environ is None else environ
    return exists("/.dockerenv") or exists("/run/.containerenv") or bool(environ.get("container"))
