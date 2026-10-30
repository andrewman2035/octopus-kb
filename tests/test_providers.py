"""P20: model providers by API key (Anthropic Messages API, OpenAI-compatible endpoints). All HTTP is respx-mocked."""
import json

import httpx
import pytest
import respx
from fakes import FakeProvider

from octopus.config import load_config, make_test_config
from octopus.db import connect
from octopus.llm.anthropic_api import ANTHROPIC_URL, AnthropicProvider, resolve_model
from octopus.llm.base import LLMError
from octopus.llm.openai_compat import OpenAICompatProvider
from octopus.llm.router import LLMRouter, build_providers
from octopus.settings import Settings

KEY = "sk-ant-secret-123"


class Sleeps:
    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)


def anthropic(key=KEY, model="haiku", **kw):
    kw.setdefault("sleep", Sleeps())
    return AnthropicProvider(f"anthropic:{model}", resolve_model(model), api_key=key, **kw)


def ok(*texts, **extra):
    return httpx.Response(200, json={"content": [{"type": "text", "text": t} for t in texts], **extra})


# ---- Anthropic ----

def test_aliases():
    assert resolve_model("haiku") == "claude-haiku-5-5"
    assert resolve_model("sonnet") == "claude-sonnet-5-5"
    assert resolve_model("opus") == "claude-opus-5-5"
    assert resolve_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"


@respx.mock
async def test_anthropic_success_sends_the_messages_request():
    route = respx.post(ANTHROPIC_URL).mock(return_value=ok(" 你好 "))
    p = anthropic(model="sonnet")
    assert p.name == "anthropic:sonnet"
    assert await p.complete("hi", "sys") == "你好"
    req = route.calls[0].request
    assert req.headers["x-api-key"] == KEY
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert req.headers["content-type"] == "application/json"
    body = json.loads(req.content)
    assert body == {"model": "claude-sonnet-5-5", "max_tokens": 8192, "system": "sys",
                    "messages": [{"role": "user", "content": "hi"}]}


@respx.mock
async def test_anthropic_concatenates_text_blocks_only():
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"content": [
        {"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]}))
    assert await anthropic().complete("hi", "s") == "第一段第二段"


@respx.mock
async def test_anthropic_key_is_looked_up_per_call():
    route = respx.post(ANTHROPIC_URL).mock(return_value=ok("x"))
    keys = iter(["k1", "k2"])
    p = anthropic(key=lambda: next(keys))
    await p.complete("a", "s")
    await p.complete("b", "s")
    assert [c.request.headers["x-api-key"] for c in route.calls] == ["k1", "k2"]


@respx.mock
async def test_anthropic_without_a_key_fails_before_any_request():
    route = respx.post(ANTHROPIC_URL).mock(return_value=ok("x"))
    for key in (None, lambda: None):
        with pytest.raises(LLMError, match="anthropic_api_key") as exc:
            await anthropic(key=key).complete("hi", "s")
        assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert not route.called


@respx.mock
@pytest.mark.parametrize("body", [
    {"content": []},
    {"content": [{"type": "text", "text": "  "}]},
    {"content": [{"type": "tool_use", "id": "x"}]},
])
async def test_anthropic_empty_text_is_llm_error(body):
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(LLMError, match="empty"):
        await anthropic().complete("hi", "s")


@respx.mock
@pytest.mark.parametrize("resp", [
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={}),
    httpx.Response(200, json={"content": "text"}),
    httpx.Response(200, json={"content": [{"type": "text", "text": 5}]}),
    httpx.Response(200, json={"content": ["x"]}),
])
async def test_anthropic_malformed_response_is_llm_error(resp):
    respx.post(ANTHROPIC_URL).mock(return_value=resp)
    with pytest.raises(LLMError, match="unexpected response"):
        await anthropic().complete("hi", "s")


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_anthropic_bad_key_is_one_chinese_hint_no_retry(status):
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(
        status, json={"type": "error", "error": {"type": "authentication_error", "message": f"invalid x-api-key {KEY}"}}))
    sleeps = Sleeps()
    with pytest.raises(LLMError) as exc:
        await anthropic(sleep=sleeps).complete("hi", "s")
    msg = str(exc.value)
    assert "key 不对" in msg and "anthropic_api_key" in msg and str(status) in msg
    assert KEY not in msg  # the body (which may echo the key) is never quoted
    assert route.call_count == 1 and sleeps.delays == []


@respx.mock
async def test_anthropic_429_waits_retry_after_then_succeeds():
    route = respx.post(ANTHROPIC_URL).mock(side_effect=[
        httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "rate"}}), ok("好了")])
    sleeps = Sleeps()
    assert await anthropic(sleep=sleeps).complete("hi", "s") == "好了"
    assert route.call_count == 2 and sleeps.delays == [7.0]


@respx.mock
async def test_529_and_5xx_back_off_then_exhaust_into_llm_error():
    route = respx.post(ANTHROPIC_URL).mock(side_effect=[
        httpx.Response(529), httpx.Response(503), httpx.Response(500, text="boom")])
    sleeps = Sleeps()
    with pytest.raises(LLMError, match="HTTP 500 after 3 tries") as exc:
        await anthropic(sleep=sleeps).complete("hi", "s")
    assert "boom" in str(exc.value)
    assert route.call_count == 3 and sleeps.delays == [2, 4]


@respx.mock
async def test_a_long_retry_after_gives_up_at_once():
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(429, headers={"retry-after": "600"}))
    sleeps = Sleeps()
    with pytest.raises(LLMError, match=r"HTTP 429 after 1 try \(retry-after 600s > 30s, not retried\)"):
        await anthropic(sleep=sleeps).complete("hi", "s")
    assert route.call_count == 1 and sleeps.delays == []


@respx.mock
async def test_an_unreadable_retry_after_uses_the_backoff():
    respx.post(ANTHROPIC_URL).mock(side_effect=[
        httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}), ok("x")])
    sleeps = Sleeps()
    assert await anthropic(sleep=sleeps).complete("hi", "s") == "x"
    assert sleeps.delays == [2]


@respx.mock
async def test_other_client_errors_are_not_retried():
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(
        400, json={"error": {"message": "model: claude-nope not found"}}))
    with pytest.raises(LLMError, match="HTTP 400.*claude-nope"):
        await anthropic().complete("hi", "s")
    assert route.call_count == 1


@respx.mock
async def test_timeouts_and_network_errors_are_llm_errors():
    respx.post(ANTHROPIC_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LLMError, match="timed out"):
        await anthropic().complete("hi", "s")
    respx.post(ANTHROPIC_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LLMError, match="ConnectError"):
        await anthropic().complete("hi", "s")


# ---- OpenAI-compatible ----

@respx.mock
async def test_openai_compat_success_with_key():
    route = respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": " 好 "}}]}))
    p = OpenAICompatProvider("openai:deepseek", "https://api.deepseek.com/v1/", "deepseek-chat",
                             api_key=lambda: "sk-d", sleep=Sleeps())
    assert await p.complete("hi", "sys") == "好"
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer sk-d"
    assert json.loads(req.content) == {"model": "deepseek-chat", "messages": [
        {"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]}


@respx.mock
async def test_openai_compat_without_a_key_sends_no_authorization():
    route = respx.post("http://127.0.0.1:1234/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    p = OpenAICompatProvider("openai:lm", "http://127.0.0.1:1234/v1", "qwen", api_key=lambda: None)
    assert await p.complete("hi", "s") == "ok"
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
async def test_openai_compat_bad_key_names_its_secret():
    respx.post("https://h/v1/chat/completions").mock(return_value=httpx.Response(401, json={"error": {"message": "bad"}}))
    p = OpenAICompatProvider("openai:deepseek", "https://h/v1", "m", api_key="sk-wrong")
    with pytest.raises(LLMError) as exc:
        await p.complete("hi", "s")
    assert "key 不对" in str(exc.value) and "deepseek_api_key" in str(exc.value)
    assert "OCTOPUS_DEEPSEEK_API_KEY" in str(exc.value) and "sk-wrong" not in str(exc.value)


@respx.mock
async def test_openai_compat_429_then_success_and_null_content():
    respx.post("https://h/v1/chat/completions").mock(side_effect=[
        httpx.Response(429, headers={"retry-after": "1.5"}),
        httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})])
    sleeps = Sleeps()
    p = OpenAICompatProvider("openai:h", "https://h/v1", "m", sleep=sleeps)
    assert await p.complete("hi", "s") == "x"
    assert sleeps.delays == [1.5]
    respx.post("https://h/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": None}}]}))
    with pytest.raises(LLMError):
        await p.complete("hi", "s")


# ---- build_providers, fallback, key lookup ----

class FakeSecrets:
    def __init__(self, **stored):
        self.stored = stored
        self.reads = []

    def get(self, name):
        self.reads.append(name)
        return self.stored.get(name)

    def find(self, name, env=True):
        value = self.get(name)
        return (value, f"钥匙串 {name}") if value is not None else (None, None)


def api_config(tmp_path, summarize=("anthropic:haiku", "openai:deepseek"), answer=("anthropic:claude-x-1", "local")):
    p = tmp_path / "c.toml"
    p.write_text(f'data_dir = "{tmp_path}"\n'
                 f'[models.summarize]\nprimary = "{summarize[0]}"\nfallback = "{summarize[1]}"\n'
                 f'[models.answer]\nprimary = "{answer[0]}"\nfallback = "{answer[1]}"\n'
                 '[providers.deepseek]\nbase_url = "https://api.deepseek.com/v1"\nmodel = "deepseek-chat"\n'
                 '[providers.unused]\nbase_url = "https://u/v1"\nmodel = "u"\n', encoding="utf-8")
    return load_config(p)


def test_build_providers_makes_api_providers(tmp_path):
    cfg = api_config(tmp_path)
    secrets = FakeSecrets()
    providers = build_providers(cfg, secrets=secrets, environ={})
    assert set(providers) == {"local", "anthropic:haiku", "anthropic:claude-x-1", "openai:deepseek"}
    assert providers["anthropic:haiku"].model == "claude-haiku-5-5"
    assert providers["anthropic:claude-x-1"].model == "claude-x-1"
    ds = providers["openai:deepseek"]
    assert (ds.name, ds.base_url, ds.model) == ("openai:deepseek", "https://api.deepseek.com/v1", "deepseek-chat")
    assert secrets.reads == []  # keys are read when a call is made, not at startup


def test_build_providers_reads_keys_env_first(tmp_path):
    cfg = api_config(tmp_path)
    secrets = FakeSecrets(anthropic_api_key="sk-chain", deepseek_api_key="sk-ds")
    providers = build_providers(cfg, secrets=secrets, environ={"ANTHROPIC_API_KEY": "sk-env"})
    assert providers["anthropic:haiku"].key() == "sk-env"
    assert providers["openai:deepseek"].key() == "sk-ds"
    assert secrets.reads == ["deepseek_api_key"]


def test_default_config_builds_no_api_providers(tmp_path):
    assert set(build_providers(make_test_config(tmp_path))) == {"claude:haiku", "claude:sonnet", "local"}


@respx.mock
async def test_exhausted_api_provider_falls_back_to_the_next_spec(tmp_path):
    cfg = api_config(tmp_path)
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(503))
    providers = build_providers(cfg, secrets=FakeSecrets(anthropic_api_key="k"), environ={})
    providers["anthropic:haiku"].sleep = Sleeps()
    providers["openai:deepseek"] = FakeProvider("openai:deepseek", "备用回答")
    router = LLMRouter(providers, {"summarize": cfg.summarize, "answer": cfg.answer},
                       Settings(connect(tmp_path / "r.db", embed_dim=8)))
    res = await router.complete("summarize", "p", "s")
    assert (res.text, res.provider, res.fell_back) == ("备用回答", "openai:deepseek", True)
    assert route.call_count == 3


@respx.mock
async def test_a_rejected_key_falls_back_and_the_key_never_reaches_the_error(tmp_path):
    cfg = api_config(tmp_path)
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(return_value=httpx.Response(401))
    providers = build_providers(cfg, secrets=FakeSecrets(deepseek_api_key="sk-ds-secret"), environ={})
    providers["local"] = FakeProvider("local", error="down")
    router = LLMRouter(providers, {"summarize": cfg.summarize, "answer": cfg.answer},
                       Settings(connect(tmp_path / "r.db", embed_dim=8)))
    with pytest.raises(LLMError) as exc:
        await router.complete("summarize", "p", "s")
    assert "anthropic_api_key" in str(exc.value) and "deepseek_api_key" in str(exc.value)
    assert "sk-ds-secret" not in str(exc.value)


def test_describe_and_local_mode_with_api_specs(tmp_path):
    from octopus.llm.router import MODEL_MODE_KEY

    cfg = api_config(tmp_path)
    settings = Settings(connect(tmp_path / "r.db", embed_dim=8))
    router = LLMRouter(build_providers(cfg, secrets=FakeSecrets(), environ={}),
                       {"summarize": cfg.summarize, "answer": cfg.answer}, settings)
    assert router.describe() == {"summarize": ["anthropic:haiku", "openai:deepseek"],
                                 "answer": ["anthropic:claude-x-1", "local"]}
    settings.set(MODEL_MODE_KEY, "local")
    assert router.describe() == {"summarize": ["local"], "answer": ["local"]}



# ---- P20 fix round 1 ----

BAD_KEYS = ["sk-x\n", " sk-x", "sk-x\t", "sk x", "sk-\x00x", "sk-ключ", "sk-’"]


@respx.mock
@pytest.mark.parametrize("key", BAD_KEYS)
async def test_a_malformed_key_is_refused_without_quoting_it(key):
    route = respx.post(ANTHROPIC_URL).mock(return_value=ok("x"))
    ds = respx.post("https://h/v1/chat/completions").mock(return_value=ok("x"))
    for p, secret in ((anthropic(key=key), "anthropic_api_key"),
                      (OpenAICompatProvider("openai:ds", "https://h/v1", "m", api_key=key), "ds_api_key")):
        with pytest.raises(LLMError) as exc:
            await p.complete("hi", "s")
        msg = str(exc.value)
        assert "空格或不该有的字符" in msg and secret in msg
        assert key.strip() not in msg and "sk-" not in msg
    assert not route.called and not ds.called


@respx.mock
async def test_error_bodies_and_transport_errors_are_scrubbed_of_the_key():
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(400, json={"error": {"message": f"bad header {KEY} here"}}))
    with pytest.raises(LLMError) as exc:
        await anthropic().complete("hi", "s")
    assert KEY not in str(exc.value) and "bad header *** here" in str(exc.value)
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(500, text=KEY * 50))
    with pytest.raises(LLMError) as exc:
        await anthropic().complete("hi", "s")
    assert KEY not in str(exc.value) and len(str(exc.value)) < 300
    for err in (httpx.LocalProtocolError(f"Illegal header value b'{KEY}'"), httpx.ConnectError(f"refused {KEY}")):
        respx.post(ANTHROPIC_URL).mock(side_effect=err)
        with pytest.raises(LLMError) as exc:
            await anthropic().complete("hi", "s")
        assert KEY not in str(exc.value) and type(err).__name__ in str(exc.value)
    respx.post("https://h/v1/chat/completions").mock(side_effect=httpx.LocalProtocolError("Illegal b'Bearer sk-d'"))
    with pytest.raises(LLMError) as exc:
        await OpenAICompatProvider("openai:h", "https://h/v1", "m", api_key="sk-d").complete("hi", "s")
    assert "sk-d" not in str(exc.value)


async def test_the_key_lookup_runs_off_the_event_loop():
    import asyncio
    import time

    ticks, seen = [], []

    def slow_key():
        time.sleep(0.3)  # a Keychain read waiting on a dialog
        seen.append(len(ticks))  # the loop kept running meanwhile
        return None

    async def ticker():
        for _ in range(5):
            ticks.append(1)
            await asyncio.sleep(0.02)

    p = anthropic(key=slow_key)
    results = await asyncio.gather(p.complete("hi", "s"), ticker(), return_exceptions=True)
    assert isinstance(results[0], LLMError) and seen[0] >= 3


@respx.mock
@pytest.mark.parametrize("status", [501, 505])
async def test_non_transient_5xx_is_not_retried(status):
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(status))
    with pytest.raises(LLMError, match=f"HTTP {status}"):
        await anthropic().complete("hi", "s")
    assert route.call_count == 1


@respx.mock
async def test_a_reply_cut_at_max_tokens_says_so_and_warns(caplog):
    from octopus.llm.api import TRUNCATED_NOTE

    respx.post(ANTHROPIC_URL).mock(return_value=ok("很长的回答", stop_reason="max_tokens"))
    with caplog.at_level("WARNING"):
        text = await anthropic().complete("hi", "s")
    assert text == "很长的回答" + TRUNCATED_NOTE
    assert len([r for r in caplog.records if "max_tokens" in r.getMessage()]) == 1
    respx.post(ANTHROPIC_URL).mock(return_value=ok("完整", stop_reason="end_turn"))
    assert await anthropic().complete("hi", "s") == "完整"
    respx.post("https://h/v1/chat/completions").mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"content": "长"}, "finish_reason": "length"}]}))
    assert await OpenAICompatProvider("openai:h", "https://h/v1", "m").complete("hi", "s") == "长" + TRUNCATED_NOTE


@respx.mock
async def test_no_key_reaches_the_log_or_the_chat_reply(tmp_path, caplog):
    """A malformed Keychain key and a service that echoes the key in a 400 body, through App → router → reply."""
    from test_app import m, make_app

    bad, echoed = "sk-ant-LEAK1\n", "sk-ds-LEAK2"
    respx.post("https://h/v1/chat/completions").mock(return_value=httpx.Response(
        400, json={"error": {"message": f"Incorrect API key provided: {echoed}"}}))
    app, ch, providers = make_app(tmp_path)
    providers["claude:sonnet"] = anthropic(key=bad, model="sonnet")
    providers["local"] = OpenAICompatProvider("openai:h", "https://h/v1", "m", api_key=echoed)
    with caplog.at_level("DEBUG"):
        await app.handle(m("复利有什么用？"))
    reply = ch.sent[-1][1]
    assert reply.startswith("现在没法回答")
    for secret in ("LEAK1", "LEAK2"):
        assert secret not in reply and secret not in caplog.text
