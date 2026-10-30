from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LLMError(Exception):
    pass


class Provider(Protocol):
    name: str

    async def complete(self, prompt: str, system: str) -> str: ...


@dataclass
class LLMResult:
    text: str
    provider: str
    fell_back: bool
    value: object = None
