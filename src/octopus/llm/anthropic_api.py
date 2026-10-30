"""Claude through the Anthropic Messages API with the user's own API key (one POST over httpx, no SDK)."""
from __future__ import annotations

import asyncio

from octopus.llm import api
from octopus.llm.base import LLMError

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ALIASES = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
# One cap for summaries and answers (a provider does not know its task); both stay far below it.
MAX_TOKENS = 8192


def resolve_model(model: str) -> str:
    """anthropic:haiku / sonnet / opus are aliases; anything else is a model id as written."""
    return ALIASES.get(model, model)


class AnthropicProvider:
    def __init__(self, name: str, model: str, api_key: api.KeySource = None, timeout: float = 300,
                 sleep=asyncio.sleep):
        self.name = name
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.sleep = sleep

    def key(self) -> str | None:
        return api.resolve_key(self.api_key)

    async def complete(self, prompt: str, system: str) -> str:
        key = await asyncio.to_thread(self.key)  # a Keychain read can wait on a dialog: never on the event loop
        if not key:
            raise api.no_key(self.name, "anthropic")
        api.check_key(self.name, "anthropic", key)
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        payload = {"model": self.model, "max_tokens": MAX_TOKENS, "system": system,
                   "messages": [{"role": "user", "content": prompt}]}
        data = await api.post_json(self.name, ANTHROPIC_URL, payload, headers, self.timeout, "anthropic", self.sleep,
                                   key=key)
        try:
            blocks = data["content"]
            if not isinstance(blocks, list):
                raise TypeError("content is not a list")
            text = "".join(b["text"] for b in blocks if b.get("type") == "text").strip()
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError(f"{self.name} returned an unexpected response: {e!r}") from e
        if not text:
            raise LLMError("empty response")
        return api.truncated(self.name, text) if data.get("stop_reason") == "max_tokens" else text
