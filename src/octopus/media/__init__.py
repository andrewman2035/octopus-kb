"""Local media processing: speech-to-text, OCR, PDF text."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from octopus.i18n import aliases, t

if TYPE_CHECKING:
    from octopus.media.asr import Transcript


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp", ".bmp", ".tiff")
AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".wav", ".aiff", ".aif", ".ogg", ".opus", ".flac", ".amr", ".silk")
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".webm")


class MediaError(Exception):
    pass


class NoSpeechError(MediaError):
    """The recording has no speech in it (music, a screen recording): nothing to transcribe."""


@dataclass
class MediaTools:
    transcribe: "Callable[[Path], Transcript]"
    ocr: Callable[[Path], str]
    pdf: Callable[[Path], str]
    attachments_dir: Path
    downloads_dir: Path
    incoming_dir: Path | None = None  # when set, only files staged under it are moved into attachments/


# a backend named in config.toml (not "auto") and the package it needs; hint when it is missing
BACKEND_PACKAGES = {"mlx": ("mlx_whisper", "mac"), "ocrmac": ("ocrmac", "mac"),
                    "faster-whisper": ("faster_whisper", "linux"), "rapidocr": ("rapidocr", "linux")}
__getattr__ = aliases({"BACKEND_MISSING": "media.backend_missing"})


def missing_backend(media) -> str | None:
    """Why a backend set explicitly in [media] cannot run here (its package is not installed), else None. Said at
    startup rather than as a raw ModuleNotFoundError on the first voice note or image."""
    import importlib.util

    for key in ("asr_backend", "ocr_backend"):
        value = getattr(media, key)
        if value in BACKEND_PACKAGES:
            package, where = BACKEND_PACKAGES[value]
            if importlib.util.find_spec(package) is None:
                hint = t("media.hint_mac_only", backend=value) if where == "mac" else t("media.hint_linux")
                return t("media.backend_missing", key=key, value=value, package=package, hint=hint)
    return None
