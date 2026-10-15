"""Tests for the TEI embedder client.

`Embedder.embed` returns one vector per input or raises; driven over a mock HTTP
transport.
"""
import dataclasses
import json

import httpx
import pytest

from kwim_api import embedder as embedder_mod
from kwim_api.config import settings
from kwim_api.embedder import Embedder


def _embedder(handler) -> Embedder:
    """A real Embedder whose HTTP client is backed by `handler`."""
    e = Embedder.__new__(Embedder)          # bypass __init__: no settings, no real socket
    e._url = "http://embedder.test"
    e._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return e


def _responds(payload, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)
    return handler


async def test_one_vector_per_input_is_returned_in_order():
    vectors = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
    e = _embedder(_responds(vectors))
    assert await e.embed(["a", "b", "c"]) == vectors
    await e.close()


async def test_short_response_raises_instead_of_truncating():
    """Two vectors for three inputs would otherwise `zip` down to two pairs,
    dropping the third input's row."""
    e = _embedder(_responds([[0.1, 0.2], [0.3, 0.4]]))
    with pytest.raises(ValueError) as exc:
        await e.embed(["a", "b", "c"])
    assert "2 vectors for 3 inputs" in str(exc.value)
    await e.close()


async def test_empty_response_raises():
    """`(await embed([x]))[0]` - the single-input form the gate and the
    knowledge and code routers use."""
    e = _embedder(_responds([]))
    with pytest.raises(ValueError) as exc:
        await e.embed(["a"])
    assert "0 vectors for 1 inputs" in str(exc.value)
    await e.close()


async def test_over_long_response_raises():
    """A longer response misattributes vectors to inputs, so it is equally a
    contract violation."""
    e = _embedder(_responds([[0.1], [0.2], [0.3]]))
    with pytest.raises(ValueError) as exc:
        await e.embed(["a", "b"])
    assert "3 vectors for 2 inputs" in str(exc.value)
    await e.close()


async def test_empty_input_is_not_a_mismatch():
    e = _embedder(_responds([]))
    assert await e.embed([]) == []
    await e.close()


async def test_http_error_still_raises_before_the_length_check():
    e = _embedder(_responds({"error": "model not loaded"}, status=503))
    with pytest.raises(httpx.HTTPStatusError):
        await e.embed(["a"])
    await e.close()


def _echoes(sizes: list[int]):
    """A handler that records each request's batch size and returns one vector
    per input, holding the input's text so order can be checked."""
    def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["inputs"]
        sizes.append(len(inputs))
        return httpx.Response(200, json=[[float(t)] for t in inputs])
    return handler


async def test_large_input_is_sent_in_batches_and_returned_in_order(monkeypatch):
    monkeypatch.setattr(embedder_mod, "settings",
                        dataclasses.replace(settings, embedder_max_batch=32))
    sizes: list[int] = []
    e = _embedder(_echoes(sizes))
    texts = [str(i) for i in range(70)]
    vectors = await e.embed(texts)
    assert sizes == [32, 32, 6]
    assert vectors == [[float(i)] for i in range(70)]
    await e.close()


async def test_a_failed_batch_raises(monkeypatch):
    monkeypatch.setattr(embedder_mod, "settings",
                        dataclasses.replace(settings, embedder_max_batch=2))
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 2:
            return httpx.Response(413, json={"error": "batch too large"})
        return httpx.Response(200, json=[[0.1], [0.2]])
    e = _embedder(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await e.embed(["a", "b", "c", "d"])
    await e.close()
