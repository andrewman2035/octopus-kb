"""User-facing text (P25): replies, notices, errors and CLI output come from one catalog per language.

zh.py is the source of truth and the default; en.py has the same keys with the same named placeholders. The
top-level `language` in config.toml picks the catalog for the whole process (load_config sets it). It only chooses
the language of what Octopus says: input is understood in both languages whatever it is."""
from __future__ import annotations

import functools
import os
import re
import subprocess
from collections.abc import Callable, Mapping

from octopus import host
from octopus.i18n import en, zh

LANGUAGES = ("zh", "en")
DEFAULT_LANGUAGE = "zh"
CATALOGS: dict[str, dict[str, str]] = {"zh": zh.MESSAGES, "en": en.MESSAGES}
_current = DEFAULT_LANGUAGE


def set_language(language: str) -> None:
    global _current
    if language not in CATALOGS:
        raise ValueError(f"unknown language {language!r}")
    _current = language


def get_language() -> str:
    return _current


def t(key: str, /, **values) -> str:
    """The text of key in the configured language, its {placeholders} filled from values (none given: as is).
    A key missing in English falls back to Chinese; an unknown key is a KeyError."""
    text = CATALOGS[_current].get(key)
    if text is None:
        text = zh.MESSAGES[key]
    return text.format(**values) if values else text


def text_in(language: str, key: str, /, **values) -> str:
    """The text of key in that language (prompts follow the text they are about, not the configured language)."""
    text = CATALOGS[language if language in CATALOGS else DEFAULT_LANGUAGE][key]
    return text.format(**values) if values else text


# lang[_REGION or -Script-REGION][.encoding][@modifier]: zh_CN.UTF-8, zh-Hans-CN, yue-Hant-HK, es-419, sr_RS@latin
_LOCALE = re.compile(r"([a-z]{2,3})(?:[_-](?:[a-z]{4}|[a-z]{2}|[0-9]{3}))*(?:\.[a-z0-9_-]+)?(?:@[a-z0-9_]+)?",
                     re.IGNORECASE)
_CHINESE = ("zh", "yue")  # Cantonese reads Chinese


def system_language(environ: Mapping[str, str], mac_ui: str | None) -> str:
    """The language a new config gets when `octopus init` is not told one (P28): "zh" for a Chinese system, "en"
    for anything else or nothing detectable (C / POSIX / unset, as in Docker). mac_ui is macOS's preferred UI
    language (None elsewhere or unreadable); it wins over the environment, whose LANG macOS terminals derive from
    the region (en_US.UTF-8 for many Chinese-UI users) and whose LC_ALL is often pinned to en_US.UTF-8 to quiet
    locale warnings. Otherwise the first of GNU LANGUAGE (its first entry), LC_ALL, LC_MESSAGES, LANG that looks
    like a locale decides; C, POSIX and junk such as "UTF-8" are skipped."""
    for value in (mac_ui, (environ.get("LANGUAGE") or "").split(":")[0], environ.get("LC_ALL"),
                  environ.get("LC_MESSAGES"), environ.get("LANG")):
        if match := _LOCALE.fullmatch((value or "").strip()):
            return "zh" if match.group(1).lower() in _CHINESE else "en"
    return "en"


def mac_ui_language(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str | None:
    """The first of macOS's preferred languages (`defaults read -g AppleLanguages`, e.g. "zh-Hans-CN"); None when
    it cannot be read (no `defaults`, a timeout, no such setting)."""
    try:
        result = run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():  # (\n    "zh-Hans-CN",\n    en\n)
        entry = line.strip().rstrip(",").strip('"')
        if entry and entry not in ("(", ")"):
            return entry
    return None


@functools.cache
def detected_language() -> str:
    """The system's language for a new config and for everything said while there is none (P28). Decided once per
    process: at most one `defaults` call, and one answer for --help, the new config and what follows."""
    return system_language(os.environ, mac_ui_language() if host.system() == host.MAC else None)


def aliases(names: Mapping[str, str | Callable[[], object]]) -> Callable[[str], object]:
    """A module-level __getattr__ that keeps old constant names readable: NAME is t(key) (or what a function given
    instead of the key returns), looked up when used, so it follows the configured language."""
    def __getattr__(name: str) -> object:
        if name in names:
            value = names[name]
            return value() if callable(value) else t(value)
        raise AttributeError(name)
    return __getattr__

