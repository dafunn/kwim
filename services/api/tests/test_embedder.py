"""Tests for the TEI embedder client.

Covers the one-to-one input/output guarantee `Embedder.embed` enforces. Drives the
real client over a mock HTTP transport: the invariant lives in the client, and
every other suite substitutes a fake Embedder that never reaches it.

Callers pair the response with the input batch positionally - by `zip`
(backfill_embeddings, rebuild) or by index (codegraph.extract) - and each of those
forms drops the tail of a short response without error.
"""
import httpx
import pytest

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
    knowledge/code routers use - would raise a bare IndexError here."""
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
