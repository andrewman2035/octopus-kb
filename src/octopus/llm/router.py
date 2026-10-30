"""Per-task primary/fallback model selection."""
from __future__ import annotations

import logging
import os
from collections.abc import Callable

from octopus.config import Config, TaskModels, parse_spec
from octopus.llm.anthropic_api import AnthropicProvider, resolve_model
from octopus.llm.base import LLMError, LLMResult, Provider
from octopus.llm.claude_cli import ClaudeCLIProvider
from octopus.llm.ollama import OllamaProvider
from octopus.llm.openai_compat import OpenAICompatProvider
from octopus.secrets import FILE_NAME as SECRETS_FILE, Secrets, find_api_key
from octopus.settings import Settings

log = logging.getLogger(__name__)
MODEL_MODE_KEY = "model_mode"
TASKS = ("summarize", "answer")


class LLMRouter:
    def __init__(self, providers: dict[str, Provider], models: dict[str, TaskModels], settings: Settings):
        self.providers = providers
        self.models = models
        self.settings = settings

    def chain(self, task: str) -> list[str]:
        if self.settings.get(MODEL_MODE_KEY) == "local":
            return ["local"]
        tm = self.models[task]
        names = [tm.primary]
        if tm.fallback and tm.fallback != tm.primary:
            names.append(tm.fallback)
        return names

    async def complete(self, task: str, prompt: str, system: str,
                       validate: Callable[[str], object] | None = None) -> LLMResult:
        errors = []
        for i, name in enumerate(self.chain(task)):
            try:
                provider = self.providers.get(name)
                if provider is None:
                    raise LLMError("provider not configured")
                text = await provider.complete(prompt, system)
                value = validate(text) if validate else None
                return LLMResult(text, name, fell_back=i > 0, value=value)
            except Exception as e:  # any provider bug must still reach the fallback
                log.warning("%s via %s failed: %s", task, name, e)
                errors.append(f"{name}: {e}")
        raise LLMError("; ".join(errors))

    def describe(self) -> dict[str, list[str]]:
        return {task: self.chain(task) for task in TASKS}


def build_providers(cfg: Config, claude_token: str | None = None, secrets: Secrets | None = None,
                    environ: dict[str, str] | None = None) -> dict[str, Provider]:
    """API keys are looked up (environment, Keychain, secrets file) when a call is made, never at startup."""
    providers: dict[str, Provider] = {
        "local": OllamaProvider(cfg.local.base_url, cfg.local.model, cfg.local.num_ctx)
    }
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": claude_token} if claude_token else None

    def key(provider: str) -> Callable[[], str | None]:
        def lookup() -> str | None:
            nonlocal secrets
            if secrets is None:
                secrets = Secrets(file=cfg.data_dir / SECRETS_FILE, environ=environ, mode=cfg.secrets_backend)
            return find_api_key(secrets, provider, environ)[0]
        return lookup

    for tm in (cfg.summarize, cfg.answer):
        for spec in (tm.primary, tm.fallback):
            if not spec or spec in providers:
                continue
            kind, arg = parse_spec(spec)
            if kind == "claude":
                providers[spec] = ClaudeCLIProvider(arg, cfg.claude_bin, cfg.llm_workdir, env=env)
            elif kind == "anthropic":
                providers[spec] = AnthropicProvider(spec, resolve_model(arg), api_key=key("anthropic"))
            elif kind == "openai":
                p = cfg.providers[arg]
                providers[spec] = OpenAICompatProvider(spec, p.base_url, p.model, api_key=key(arg))
    return providers
