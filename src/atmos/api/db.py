"""Database access for the API.

Read only. The API connects as `atmos_api`, which has select on the tables it
needs and nothing else. It cannot insert, cannot update, and could not alter an
observation even if a request found a way to ask it to.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# Queries are small and indexed. A large pool would only queue work differently.
POOL_MIN = 1
POOL_MAX = 8

# A request that cannot be served quickly is better refused than left hanging.
STATEMENT_TIMEOUT_MS = 15_000

_pool: AsyncConnectionPool | None = None


def dsn() -> str:
    value = os.environ.get("ATMOS_API_DATABASE_URL") or os.environ.get(
        "ATMOS_DATABASE_URL"
    )
    if not value:
        raise RuntimeError(
            "no database DSN, set ATMOS_API_DATABASE_URL or ATMOS_DATABASE_URL"
        )
    return value


async def open_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            dsn(), min_size=POOL_MIN, max_size=POOL_MAX, open=False,
            kwargs={"row_factory": dict_row},
        )
        await _pool.open(wait=True, timeout=10)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def cursor() -> AsyncIterator[Any]:
    pool = await open_pool()
    async with pool.connection() as conn:
        # Belt and braces. The role has no write grants, and the transaction
        # refuses writes anyway.
        await conn.set_read_only(True)
        async with conn.cursor() as cur:
            await cur.execute(f"set local statement_timeout = {STATEMENT_TIMEOUT_MS}")
            # One clock. Timestamps come back at Bosnia and Herzegovina local
            # time, and each source carries the timezone it published in so the
            # original can be shown beside it.
            await cur.execute("set local timezone = 'Europe/Sarajevo'")
            yield cur


async def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with cursor() as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    return list(rows)


async def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    async with cursor() as cur:
        await cur.execute(sql, params)
        row = await cur.fetchone()
    return dict(row) if row else None
