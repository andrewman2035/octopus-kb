"""Turn fetched text into title / summary / points / tags."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from octopus import i18n
from octopus.i18n import text_in
from octopus.llm.base import LLMError
from octopus.llm.router import LLMRouter
from octopus.text import text_language

MAX_CHARS = 40000
SYSTEM = text_in("zh", "prompt.summarize.system")  # the Chinese prompt's parts (the catalog holds both languages)
INSTRUCTIONS = text_in("zh", "prompt.summarize.instructions")


def summary_language(setting: str, text: str) -> str:
    """The language a summary is written in: config's summary_language ("zh" / "en"); "auto" (opt-in) the content's
    own, judged with a bias to the configured language; unset ("") the configured language."""
    if setting in ("zh", "en"):
        return setting
    if setting == "auto":
        return text_language(text, i18n.get_language())
    return i18n.get_language()


@dataclass
class Summary:
    title: str
    summary: str
    points: list[str]
    tags: list[str]
    language: str
    provider: str
    fell_back: bool


def parse_json_object(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError(f"model did not return JSON: {text[:200]!r}")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"model returned invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LLMError("model JSON is not an object")
    return data


def _as_list(value: object) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,，、；;\n]", value)  # zh: content
    elif not isinstance(value, list):
        return []
    return [s for s in (re.sub(r"\s*[\r\n]+\s*", " ", str(v)).strip() for v in value) if s]


def _validate(text: str) -> dict:
    data = parse_json_object(text)
    if not str(data.get("summary") or "").strip():
        raise LLMError("model JSON has no summary")
    return data


async def summarize(llm: LLMRouter, text: str, title_hint: str | None = None, language: str = "") -> Summary:
    """language: config's summary_language ("auto": the content's language; "": the configured language)."""
    lang = summary_language(language, text)
    prompt = text_in(lang, "prompt.summarize.input", instructions=text_in(lang, "prompt.summarize.instructions"),
                     title=title_hint or text_in(lang, "prompt.none"), text=text[:MAX_CHARS])
    res = await llm.complete("summarize", prompt, text_in(lang, "prompt.summarize.system"), validate=_validate)
    data = res.value
    return Summary(
        title=str(data.get("title") or title_hint or "").strip(),
        summary=str(data.get("summary", "")).strip(),
        points=_as_list(data.get("points")),
        tags=[t for t in (s.lstrip("#").strip() for s in _as_list(data.get("tags"))) if t],
        language=str(data.get("language") or "").strip() or "zh",
        provider=res.provider,
        fell_back=res.fell_back,
    )
