"""Local model through Ollama's native chat API (thinking off, explicit context size)."""
from __future__ import annotations

import httpx

from octopus.llm.base import LLMError


class OllamaProvider:
    name = "local"

    def __init__(self, base_url: str, model: str, num_ctx: int, timeout: float = 600):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.timeout = timeout

    async def complete(self, prompt: str, system: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "think": False,
            "options": {"num_ctx": self.num_ctx},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(f"{self.base_url}/api/chat", json=payload)
                r.raise_for_status()
            text = r.json()["message"]["content"].strip()
        except httpx.HTTPError as e:
            raise LLMError(f"local model failed: {e!r}") from e
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
            raise LLMError(f"local model returned an unexpected response: {e!r}") from e
        if not text:
            raise LLMError("empty response")
        return text
