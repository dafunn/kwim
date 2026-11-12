"""Ids from a URL that are not UUIDs: no match (so the route answers 404), not
an error from the uuid column."""
import pytest

from kwim_api.stores.admin import AdminStore
from kwim_api.stores.pg_pool import is_uuid
from kwim_api.stores.postgres import PostgresStore


@pytest.mark.parametrize("value, ok", [
    ("00000000-0000-0000-0000-000000000000", True),
    ("2d4b1c3e-5f60-4a71-8b92-a3c4d5e6f708", True),
    ("not-a-uuid", False),
    ("None", False),
    ("", False),
])
def test_is_uuid(value, ok):
    assert is_uuid(value) is ok


async def test_get_pending_with_a_malformed_id_is_none():
    store = PostgresStore()
    store._pool = None  # never touched: the id is refused first
    assert await store.get_pending("acme", "not-a-uuid") is None


async def test_claim_pending_with_a_malformed_id_is_none():
    store = PostgresStore()
    store._pool = None
    assert await store.claim_pending("acme", "not-a-uuid", "approved", "k1", "api") is None


async def test_get_job_with_a_malformed_id_is_none():
    store = AdminStore()
    store._pool = None
    assert await store.get_job("None") is None


async def test_get_document_with_a_malformed_id_is_none():
    store = PostgresStore()
    store._pool = None
    assert await store.get_document("acme", "not-a-uuid") is None
    assert await store.get_revision("acme", "not-a-uuid") is None
