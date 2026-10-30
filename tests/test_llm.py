import json
import stat
import sys

import httpx
import pytest
import respx
from fakes import FakeProvider

from octopus.config import TaskModels, make_test_config
from octopus.db import connect
from octopus.llm.base import LLMError, LLMResult
from octopus.llm.claude_cli import ClaudeCLIProvider
from octopus.llm.ollama import OllamaProvider
from octopus.llm.openai_compat import OpenAICompatProvider
from octopus.llm.router import MODEL_MODE_KEY, LLMRouter, build_providers
from octopus.settings import Settings


def fake_claude(tmp_path, body: str):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\nimport sys, json, time\nargs = sys.argv[1:]\nprompt = sys.stdin.read()\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


async def test_claude_cli_success(tmp_path):
    bin_ = fake_claude(tmp_path, (
        "assert args[:3] == ['-p', '--output-format', 'json']\n"
        "for flag in ['--tools', '--system-prompt', '--strict-mcp-config', '--setting-sources',"
        " '--disable-slash-commands', '--no-session-persistence']: assert flag in args, flag\n"
        "assert '--bare' not in args\n"
        "assert args[args.index('--tools') + 1] == ''\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
        " 'result': 'ECHO:' + prompt + '|' + args[args.index('--model') + 1] + '|' + args[args.index('--system-prompt') + 1]}))"
    ))
    p = ClaudeCLIProvider("haiku", claude_bin=bin_, workdir=tmp_path / "wd")
    assert p.name == "claude:haiku"
    assert await p.complete("hi", "sys") == "ECHO:hi|haiku|sys"
    assert (tmp_path / "wd").is_dir()


async def test_claude_cli_error_result(tmp_path):
    bin_ = fake_claude(tmp_path, "print(json.dumps({'subtype': 'success', 'is_error': True, 'result': 'usage limit reached'}))")
    with pytest.raises(LLMError, match="usage limit"):
        await ClaudeCLIProvider("haiku", claude_bin=bin_, workdir=tmp_path).complete("hi", "s")


async def test_claude_cli_garbage_output(tmp_path):
    bin_ = fake_claude(tmp_path, "sys.stderr.write('not logged in'); sys.exit(1)")
    with pytest.raises(LLMError, match="not logged in"):
        await ClaudeCLIProvider("haiku", claude_bin=bin_, workdir=tmp_path).complete("hi", "s")


async def test_claude_cli_missing_binary(tmp_path):
    with pytest.raises(LLMError):
        await ClaudeCLIProvider("haiku", claude_bin=str(tmp_path / "nope"), workdir=tmp_path).complete("hi", "s")


async def test_claude_cli_timeout(tmp_path):
    bin_ = fake_claude(tmp_path, "time.sleep(5)")
    with pytest.raises(LLMError, match="timed out"):
        await ClaudeCLIProvider("haiku", claude_bin=bin_, workdir=tmp_path, timeout=0.5).complete("hi", "s")


@respx.mock
async def test_ollama_provider():
    route = respx.post("http://h/api/chat").mock(return_value=httpx.Response(200, json={"message": {"content": " ok "}}))
    p = OllamaProvider("http://h", "qwen", num_ctx=1234)
    assert p.name == "local"
    assert await p.complete("hi", "sys") == "ok"
    body = json.loads(route.calls[0].request.content)
    assert body["think"] is False and body["stream"] is False
    assert body["options"] == {"num_ctx": 1234}
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


@respx.mock
async def test_ollama_provider_errors():
    respx.post("http://h/api/chat").mock(return_value=httpx.Response(500))
    with pytest.raises(LLMError):
        await OllamaProvider("http://h", "qwen", 10).complete("hi", "s")
    respx.post("http://h2/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LLMError):
        await OllamaProvider("http://h2", "qwen", 10).complete("hi", "s")


@respx.mock
async def test_openai_compat_provider():
    route = respx.post("http://h/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "yo"}}]}))
    p = OpenAICompatProvider("deepseek", "http://h/v1", "m", api_key="k")
    assert await p.complete("hi", "s") == "yo"
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


def make_router(tmp_path, providers, primary="claude:haiku", fallback="local"):
    settings = Settings(connect(tmp_path / "r.db", embed_dim=8))
    models = {"summarize": TaskModels(primary, fallback), "answer": TaskModels(primary, fallback)}
    return LLMRouter({p.name: p for p in providers}, models, settings), settings


async def test_router_primary(tmp_path):
    r, _ = make_router(tmp_path, [FakeProvider("claude:haiku", "A"), FakeProvider("local", "B")])
    assert await r.complete("summarize", "p", "s") == LLMResult("A", "claude:haiku", False)


async def test_router_fallback(tmp_path):
    local = FakeProvider("local", "B")
    r, _ = make_router(tmp_path, [FakeProvider("claude:haiku", error="limit"), local])
    res = await r.complete("answer", "p", "s")
    assert (res.text, res.provider, res.fell_back) == ("B", "local", True)
    assert local.calls == [("p", "s")]


async def test_router_all_fail(tmp_path):
    r, _ = make_router(tmp_path, [FakeProvider("claude:haiku", error="limit"), FakeProvider("local", error="down")])
    with pytest.raises(LLMError, match="limit.*down"):
        await r.complete("answer", "p", "s")


async def test_router_local_mode(tmp_path):
    claude = FakeProvider("claude:haiku", "A")
    r, settings = make_router(tmp_path, [claude, FakeProvider("local", "B")])
    settings.set(MODEL_MODE_KEY, "local")
    assert r.chain("answer") == ["local"]
    assert (await r.complete("answer", "p", "s")).text == "B"
    assert claude.calls == []
    assert r.describe() == {"summarize": ["local"], "answer": ["local"]}


def test_router_no_duplicate_fallback(tmp_path):
    r, _ = make_router(tmp_path, [FakeProvider("local", "B")], primary="local", fallback="local")
    assert r.chain("summarize") == ["local"]


def test_build_providers(tmp_path):
    cfg = make_test_config(tmp_path)
    providers = build_providers(cfg)
    assert set(providers) == {"claude:haiku", "claude:sonnet", "local"}
    assert providers["claude:sonnet"].model == "sonnet"
    assert providers["claude:haiku"].workdir == cfg.llm_workdir


@respx.mock
async def test_ollama_unexpected_body_is_llm_error():
    respx.post("http://h/api/chat").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(LLMError):
        await OllamaProvider("http://h", "qwen", 10).complete("hi", "s")
    respx.post("http://h2/api/chat").mock(return_value=httpx.Response(200, content=b"not json"))
    with pytest.raises(LLMError):
        await OllamaProvider("http://h2", "qwen", 10).complete("hi", "s")


@respx.mock
async def test_openai_compat_unexpected_body_is_llm_error():
    respx.post("http://h/v1/chat/completions").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(LLMError):
        await OpenAICompatProvider("ds", "http://h/v1", "m").complete("hi", "s")
    respx.post("http://h2/v1/chat/completions").mock(return_value=httpx.Response(200, json={"choices": []}))
    with pytest.raises(LLMError):
        await OpenAICompatProvider("ds", "http://h2/v1", "m").complete("hi", "s")


@respx.mock
async def test_empty_response_is_llm_error():
    respx.post("http://h/api/chat").mock(return_value=httpx.Response(200, json={"message": {"content": "  "}}))
    with pytest.raises(LLMError, match="empty"):
        await OllamaProvider("http://h", "qwen", 10).complete("hi", "s")
    respx.post("http://h/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}))
    with pytest.raises(LLMError, match="empty"):
        await OpenAICompatProvider("ds", "http://h/v1", "m").complete("hi", "s")


async def test_claude_cli_non_object_json(tmp_path):
    bin_ = fake_claude(tmp_path, "print(json.dumps([1, 2]))")
    with pytest.raises(LLMError):
        await ClaudeCLIProvider("haiku", claude_bin=bin_, workdir=tmp_path).complete("hi", "s")
    bin2 = fake_claude(tmp_path, "print('null')")
    with pytest.raises(LLMError):
        await ClaudeCLIProvider("haiku", claude_bin=bin2, workdir=tmp_path).complete("hi", "s")


async def test_claude_cli_empty_result(tmp_path):
    bin_ = fake_claude(tmp_path, "print(json.dumps({'subtype': 'success', 'is_error': False, 'result': '  '}))")
    with pytest.raises(LLMError, match="empty"):
        await ClaudeCLIProvider("haiku", claude_bin=bin_, workdir=tmp_path).complete("hi", "s")


async def test_claude_cli_kills_child_on_cancel(tmp_path):
    import asyncio
    pidfile = tmp_path / "pid"
    bin_ = fake_claude(tmp_path, f"import os\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(30)")
    task = asyncio.create_task(ClaudeCLIProvider("haiku", claude_bin=bin_, workdir=tmp_path).complete("hi", "s"))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if pidfile.exists() and pidfile.read_text():
            break
    pid = int(pidfile.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    import os
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_router_validator_rejects_primary(tmp_path):
    local = FakeProvider("local", "good")
    r, _ = make_router(tmp_path, [FakeProvider("claude:haiku", "bad"), local])

    def validate(text):
        if text != "good":
            raise LLMError("not good")
        return text.upper()

    res = await r.complete("summarize", "p", "s", validate=validate)
    assert (res.text, res.provider, res.fell_back, res.value) == ("good", "local", True, "GOOD")


async def test_router_validator_all_rejected(tmp_path):
    r, _ = make_router(tmp_path, [FakeProvider("claude:haiku", "bad"), FakeProvider("local", "bad")])

    def validate(text):
        raise LLMError("nope")

    with pytest.raises(LLMError, match="claude:haiku: nope.*local: nope"):
        await r.complete("summarize", "p", "s", validate=validate)


async def test_router_unregistered_provider(tmp_path):
    r, _ = make_router(tmp_path, [FakeProvider("local", "B")])  # claude:haiku not registered
    res = await r.complete("summarize", "p", "s")
    assert (res.text, res.fell_back) == ("B", True)
    r2, _ = make_router(tmp_path, [], fallback=None)
    with pytest.raises(LLMError, match="claude:haiku"):
        await r2.complete("summarize", "p", "s")


async def test_claude_cli_undecodable_output_is_llm_error(tmp_path):
    bin_ = fake_claude(tmp_path, "sys.stdout.buffer.write(b'\\x80\\x80\\x80'); sys.exit(1)")
    with pytest.raises(LLMError):
        await ClaudeCLIProvider("haiku", claude_bin=bin_, workdir=tmp_path).complete("hi", "s")


class CrashingProvider:
    name = "claude:haiku"

    async def complete(self, prompt: str, system: str) -> str:
        raise UnicodeDecodeError("utf-8", b"\x80", 0, 1, "invalid start byte")


async def test_router_falls_back_on_unexpected_provider_exception(tmp_path):
    local = FakeProvider("local", "B")
    r, _ = make_router(tmp_path, [CrashingProvider(), local])
    res = await r.complete("answer", "p", "s")
    assert (res.text, res.provider, res.fell_back) == ("B", "local", True)


async def test_router_all_fail_with_unexpected_exception_raises_llm_error(tmp_path):
    r, _ = make_router(tmp_path, [CrashingProvider(), FakeProvider("local", error="down")])
    with pytest.raises(LLMError, match="down"):
        await r.complete("answer", "p", "s")


async def test_claude_cli_passes_env(tmp_path):
    bin_ = fake_claude(tmp_path, "import os\nprint(json.dumps({'subtype': 'success', 'is_error': False, "
                                 "'result': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', 'none')}))")
    p = ClaudeCLIProvider("haiku", claude_bin=bin_, workdir=tmp_path, env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "PATH": "/usr/bin:/bin"})
    assert await p.complete("hi", "s") == "tok"


def test_build_providers_with_token(tmp_path):
    cfg = make_test_config(tmp_path)
    p = build_providers(cfg, claude_token="tok")["claude:haiku"]
    assert p.env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert build_providers(cfg)["claude:haiku"].env is None
