"""Test doubles shared across tests."""
from __future__ import annotations

import json
import math
import zlib
from typing import Callable

from octopus.embed import EmbedError
from octopus.fetchers.base import FetchResult
from octopus.index.tokenizer import tokenize
from octopus.llm.base import LLMError
from octopus.media import MediaError, MediaTools
from octopus.media.asr import Transcript


class FakeEmbedder:
    """Bag-of-words hashed into 16 dims: similar text → similar vectors."""

    dim = 16

    def __init__(self):
        self.fail = False
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise EmbedError("embedder down")
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in tokenize(t):
                v[zlib.crc32(tok.encode()) % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


class FakeProvider:
    def __init__(self, name: str, respond: str | Callable[[str], str] = "", error: str | None = None):
        self.name = name
        self.respond = respond
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def complete(self, prompt: str, system: str) -> str:
        self.calls.append((prompt, system))
        if self.error:
            raise LLMError(self.error)
        return self.respond(prompt) if callable(self.respond) else self.respond



def summary_json(title: str = "标题", tags=("AI",)) -> str:
    return json.dumps({"title": title, "summary": "这是摘要。", "points": ["要点一", "要点二"],
                        "tags": list(tags), "language": "zh"}, ensure_ascii=False)


class StubFetcher:
    name = "stub"

    def __init__(self, result: FetchResult | Exception, handles: str = ""):
        self.result = result
        self.handles = handles
        self.calls: list[str] = []

    def can_handle(self, url: str) -> bool:
        return self.handles in url

    def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeChannel:
    def __init__(self, name: str = "cli", acks_saves: bool = True):
        self.name = name
        self.acks_saves = acks_saves
        self.sent: list[tuple[str, str]] = []
        self.sent_full: list[tuple[str, list]] = []
        self._n = 0

    async def start(self, on_message) -> None:
        return None

    async def send(self, user_id: str, reply):
        from octopus.channels.base import SendResult

        self._n += 1
        self.sent.append((user_id, reply.text))
        self.sent_full.append((reply.text, list(reply.buttons)))
        return SendResult(True, f"m{self._n}")


def fake_media(tmp_path, transcript="转写稿", ocr="图片文字", pdf="PDF 文字", fail=None) -> MediaTools:
    calls: list[tuple[str, str]] = []

    def maybe_fail(kind):
        if fail == kind:
            raise MediaError(f"{kind} 失败")

    def transcribe(path):
        calls.append(("asr", str(path)))
        maybe_fail("asr")
        return Transcript(transcript, "zh")

    def do_ocr(path):
        calls.append(("ocr", str(path)))
        maybe_fail("ocr")
        return ocr

    def do_pdf(path):
        calls.append(("pdf", str(path)))
        maybe_fail("pdf")
        return pdf

    tools = MediaTools(transcribe=transcribe, ocr=do_ocr, pdf=do_pdf,
                       attachments_dir=tmp_path / "attachments", downloads_dir=tmp_path / "downloads",
                       incoming_dir=tmp_path / "attachments" / "_incoming")
    tools.calls = calls
    return tools
