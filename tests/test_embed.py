import json

import httpx
import pytest
import respx

from octopus.embed import EmbedError, OllamaEmbedder


@respx.mock
async def test_ollama_embedder_batches():
    route = respx.post("http://h/api/embed").mock(
        side_effect=lambda req: httpx.Response(200, json={"embeddings": [[0.1, 0.2]] * len(json.loads(req.content)["input"])})
    )
    emb = OllamaEmbedder("http://h", "bge-m3", dim=2, batch_size=2)
    out = await emb.embed(["a", "b", "c"])
    assert len(out) == 3
    assert route.call_count == 2


@respx.mock
async def test_ollama_embedder_error():
    respx.post("http://h/api/embed").mock(return_value=httpx.Response(500))
    with pytest.raises(EmbedError):
        await OllamaEmbedder("http://h", "bge-m3", dim=2).embed(["a"])


def _emb(**kw):
    return OllamaEmbedder("http://h", "bge-m3", dim=2, **kw)


@respx.mock
async def test_ollama_embedder_request_payload():
    route = respx.post("http://h/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})
    )
    out = await OllamaEmbedder("http://h/", "bge-m3", dim=2).embed(["a", "b"])
    assert out == [[0.1, 0.2], [0.3, 0.4]]
    req = route.calls.last.request
    assert str(req.url) == "http://h/api/embed"
    assert json.loads(req.content) == {"model": "bge-m3", "input": ["a", "b"]}


@respx.mock
@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, json={"nope": 1}),
    httpx.Response(200, json={"embeddings": 5}),
    httpx.Response(200, json={"embeddings": [[0.1, 0.2]]}),  # wrong count (2 texts)
    httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.1]]}),  # wrong dim
    httpx.Response(200, json={"embeddings": [[0.1, 0.2], None]}),
])
async def test_ollama_embedder_bad_response(response):
    respx.post("http://h/api/embed").mock(return_value=response)
    with pytest.raises(EmbedError):
        await _emb().embed(["a", "b"])


@respx.mock
async def test_ollama_embedder_connection_error():
    respx.post("http://h/api/embed").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(EmbedError):
        await _emb().embed(["a"])
