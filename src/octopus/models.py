"""Channel-independent message types."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Attachment:
    kind: str  # image / audio / video / file
    path: Path | None = None
    filename: str | None = None
    mime: str | None = None
    text: str | None = None  # text the platform already extracted, e.g. WeChat voice transcript
    size: int | None = None
    voice: bool = False  # a hold-to-talk recording (not an audio file): handled as the words said


@dataclass
class IncomingMessage:
    channel: str
    user_id: str
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    message_id: str = ""
    reply_to_id: str | None = None
    callback: str | None = None  # Button.data of a pressed button


def attachment_to_dict(a: Attachment) -> dict:
    return {"kind": a.kind, "path": str(a.path) if a.path else None, "filename": a.filename,
            "mime": a.mime, "text": a.text, "size": a.size, "voice": a.voice}


def attachment_from_dict(d: dict) -> Attachment:
    return Attachment(kind=d["kind"], path=Path(d["path"]) if d.get("path") else None, filename=d.get("filename"),
                      mime=d.get("mime"), text=d.get("text"), size=d.get("size"),
                      voice=d.get("voice", False))
