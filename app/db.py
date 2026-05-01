"""Async Postgres pool helpers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request

from app.config import get_settings


class SchemaPool:
    """Asyncpg pool wrapper that sets search_path on the connection used by each operation."""

    def __init__(self, pool: Any, schema: str) -> None:
        self._pool = pool
        self._schema = schema

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)

    async def _prepare(self, connection: Any) -> None:
        if self._schema != "public":
            await connection.execute(f"SET LOCAL search_path TO {self._schema}, public")

    def acquire(self) -> Any:
        return SchemaAcquireContext(self)

    async def close(self) -> None:
        await self._pool.close()

    async def execute(self, sql: str, *args) -> str:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._prepare(connection)
                return await connection.execute(sql, *args)

    async def fetch(self, sql: str, *args) -> list[Any]:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._prepare(connection)
                return await connection.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args) -> Any:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._prepare(connection)
                return await connection.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args) -> Any:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._prepare(connection)
                return await connection.fetchval(sql, *args)


class SchemaAcquireContext:
    def __init__(self, schema_pool: SchemaPool) -> None:
        self._schema_pool = schema_pool
        self._inner = None
        self._connection = None

    async def __aenter__(self) -> Any:
        self._inner = self._schema_pool._pool.acquire()
        self._connection = await self._inner.__aenter__()
        return self._connection

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return await self._inner.__aexit__(exc_type, exc, tb)


@asynccontextmanager
async def db_lifespan(app: Any) -> AsyncIterator[None]:
    """Create an asyncpg pool for the app lifetime."""
    import asyncpg

    settings = get_settings()
    raw_pool = await asyncpg.create_pool(settings.database_url, statement_cache_size=0)
    pool = SchemaPool(raw_pool, settings.database_schema)
    app.state.pool = pool
    try:
        yield
    finally:
        await pool.close()


def get_pool(request: Request) -> Any:
    """FastAPI dependency returning the application database pool."""
    return request.app.state.pool


async def ping(pool: Any) -> None:
    """Raise if the database is not reachable."""
    async with pool.acquire() as connection:
        await connection.execute("SELECT 1")
