"""Claude via the Claude Code CLI on the user's subscription."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from octopus.llm.base import LLMError


class ClaudeCLIProvider:
    def __init__(self, model: str, claude_bin: str, workdir: Path, timeout: float = 300,
                 env: dict[str, str] | None = None):
        self.model = model
        self.name = f"claude:{model}"
        self.claude_bin = claude_bin
        self.workdir = workdir
        self.timeout = timeout
        self.env = env  # None inherits the parent environment

    def _args(self, system: str) -> list[str]:
        return [
            self.claude_bin, "-p", "--output-format", "json", "--model", self.model,
            "--tools", "", "--system-prompt", system, "--strict-mcp-config",
            "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
        ]

    async def complete(self, prompt: str, system: str) -> str:
        self.workdir.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._args(system), cwd=self.workdir, env=self.env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise LLMError(f"cannot run claude: {e}") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), self.timeout)
        except TimeoutError:
            raise LLMError(f"claude timed out after {self.timeout}s") from None
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        try:
            data = json.loads(out)
        except ValueError:  # JSONDecodeError and UnicodeDecodeError
            detail = (err.decode(errors="replace") or out.decode(errors="replace"))[-300:]
            raise LLMError(f"claude exit {proc.returncode}: {detail}") from None
        if not isinstance(data, dict):
            raise LLMError(f"claude returned unexpected output: {out[:200]!r}")
        if data.get("is_error") or data.get("subtype") != "success":
            raise LLMError(str(data.get("result") or data.get("subtype") or "unknown claude error"))
        text = str(data.get("result") or "").strip()
        if not text:
            raise LLMError("empty response")
        return text
