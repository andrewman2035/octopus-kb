"""Reply texts shown to the user."""
from __future__ import annotations

from octopus.i18n import aliases, t
from octopus.pipeline import SaveOutcome
from octopus.qa import Answer

__getattr__ = aliases({"ACK": "formatting.ack", "MERGE_ASK": "formatting.merge_ask",
                       "SUPPLEMENT_ASK": "formatting.supplement_ask", "LOCAL_NOTE_ANSWER": "formatting.local_answer",
                       "LOCAL_NOTE_SUMMARY": "formatting.local_summary"})


def ack() -> str:
    return t("formatting.ack")


def _tags(tags: list[str]) -> str:
    return " ".join(f"#{tag}" for tag in tags)


def format_saved(o: SaveOutcome) -> str:
    e = o.entry
    if e.status == "needs_manual":
        lines = [t("formatting.not_fetched", what=e.url or e.title)]
        if o.note:
            lines.append(t("formatting.reason", note=o.note))
        lines += [t("formatting.merge_ask"), t("common.label", label="id", value=e.id)]
        return "\n".join(lines)
    if o.merged:
        head = t("formatting.merged", title=e.title)
    else:
        head = t("formatting.saved" if e.status == "ok" else "formatting.saved_partial", title=e.title)
    lines = [head, t("formatting.filled" if e.status == "ok" else "formatting.filled_partial")] if o.filled else [head]
    if e.summary:
        lines.append(e.summary)
    if e.tags:
        lines.append(_tags(e.tags))
    if e.status == "partial":
        note = o.note or t("formatting.partial_note")
        lines.append(note if o.summary_failed else t("formatting.partial_ask", note=note, ask=t("formatting.supplement_ask")))
    elif o.summary_failed and o.note:
        lines.append(o.note)
    lines.append(t("common.label", label="id", value=e.id))
    if o.local_fallback:
        lines.append(t("formatting.local_summary"))
    return "\n".join(lines)


def format_duplicate(entry_id: str, title: str) -> str:
    return t("formatting.duplicate", title=title, id=entry_id)


def format_answer(a: Answer) -> str:
    parts = [a.text]
    if a.sources:
        parts.append(t("formatting.sources") + "\n" + "\n".join(
            t("formatting.source", n=s.n, title=s.title, id=s.entry_id) + (f" {s.url}" if s.url else "")
            for s in a.sources))
    if a.fell_back and a.provider == "local":
        parts.append(t("formatting.local_answer"))
    return "\n\n".join(parts)


def format_job_failed(error: str) -> str:
    return t("formatting.job_failed", error=error)
