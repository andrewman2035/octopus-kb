"""Text embeddings via the local Ollama server (bge-m3, 1024 dims, cross-lingual)."""
from __future__ import annotations

from typing import Protocol

import httpx


class EmbedError(Exception):
    pass


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, dim: int, batch_size: int = 16, timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self.timeout = timeout

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for i in range(0, len(texts), self.batch_size):
                try:
                    r = await client.post(f"{self.base_url}/api/embed",
                                          json={"model": self.model, "input": texts[i:i + self.batch_size]})
                    r.raise_for_status()
                except httpx.HTTPError as e:
                    raise EmbedError(f"embedding failed: {e}") from e
                try:
                    out.extend([float(x) for x in v] for v in r.json()["embeddings"])
                except (ValueError, KeyError, TypeError) as e:
                    raise EmbedError(f"malformed embedding response: {e!r}") from e
        if len(out) != len(texts):
            raise EmbedError(f"expected {len(texts)} embeddings, got {len(out)}")
        if any(len(v) != self.dim for v in out):
            raise EmbedError(f"embedding dimension mismatch: expected {self.dim}")
        return out
