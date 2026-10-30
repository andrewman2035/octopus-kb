"""Any OpenAI-compatible endpoint, named in [providers.<name>] and used as "openai:<name>"
(DeepSeek, Qwen's DashScope compatible mode, OpenRouter, LM Studio…)."""
from __future__ import annotations

import asyncio

from octopus.llm import api
from octopus.llm.base import LLMError


class OpenAICompatProvider:
    def __init__(self, name: str, base_url: str, model: str, api_key: api.KeySource = None, timeout: float = 300,
                 sleep=asyncio.sleep):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.sleep = sleep

    def key(self) -> str | None:
        return api.resolve_key(self.api_key)

    async def complete(self, prompt: str, system: str) -> str:
        key = await asyncio.to_thread(self.key)  # a Keychain read can wait on a dialog: never on the event loop
        provider = self.name.removeprefix("openai:")
        if key:
            api.check_key(self.name, provider, key)
        headers = {"Authorization": f"Bearer {key}"} if key else {}  # no key: a local server (LM Studio…)
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        data = await api.post_json(self.name, f"{self.base_url}/chat/completions", payload, headers, self.timeout,
                                   provider, self.sleep, key=key)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError(f"{self.name} returned an unexpected response: {e!r}") from e
        if not text:
            raise LLMError("empty response")
        return api.truncated(self.name, text) if choice.get("finish_reason") == "length" else text
