"""Shared Postgres connection pool for PostgresStore and AdminStore.

Each connection is checked on checkout; one the server dropped is replaced.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg.rows import AsyncRowFactory
from psycopg_pool import AsyncConnectionPool

from ..config import settings


async def open_pool(name: str) -> AsyncConnectionPool:
    """Open a pool and wait for its first min_size connections.

    Discrete arguments, not a URL. Connections run autocommit=True.
    """
    pool = AsyncConnectionPool(
        name=name,
        kwargs={
            "host": settings.pg_host, "port": settings.pg_port, "dbname": settings.pg_db,
            "user": settings.pg_user, "password": settings.pg_password, "autocommit": True,
        },
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
        timeout=settings.pg_pool_timeout_s,
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
    await pool.open(wait=True)
    return pool


@asynccontextmanager
async def cursor(pool: AsyncConnectionPool, row_factory: AsyncRowFactory | None = None) -> AsyncIterator:
    """Borrow a connection for the duration of one cursor."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=row_factory) as cur:
            yield cur
