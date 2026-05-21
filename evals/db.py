from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
DURABLE_MIGRATION_FILENAMES = {"0006_plan7_eval_results.sql"}
PRODUCTION_HOST_MARKERS = (
    "supabase.co",
    "supabase.net",
    "railway.app",
    "render.com",
    "neon.tech",
    "amazonaws.com",
)
PRODUCTION_NAME_MARKERS = ("prod", "production")
SAFE_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class EvalDatabaseSafetyError(RuntimeError):
    """Raised when an eval would run against a production-like database."""


@dataclass(frozen=True)
class ScratchDatabase:
    pool: Any
    schema: str


def ensure_safe_database_url(database_url: str, *, allow_production: bool = False) -> None:
    if allow_production:
        return
    if not database_url:
        raise EvalDatabaseSafetyError("EVAL_DATABASE_URL or DATABASE_URL is required for eval DB setup")
    parsed = urlparse(database_url)
    host = (parsed.hostname or "").lower()
    database_name = (parsed.path or "").lstrip("/").lower()
    full_url = database_url.lower()
    if host in SAFE_LOCAL_HOSTS:
        return
    if any(marker in host for marker in PRODUCTION_HOST_MARKERS):
        raise EvalDatabaseSafetyError(
            "refusing to run evals against a production-like managed database host; "
            "set EVAL_ALLOW_PRODUCTION_DATABASE=1 only for an explicitly approved eval target"
        )
    if any(marker in database_name for marker in ("test", "eval", "local", "dev")):
        return
    if any(marker in full_url for marker in PRODUCTION_NAME_MARKERS):
        raise EvalDatabaseSafetyError(
            "refusing to run evals against a database URL containing prod/production markers"
        )
    raise EvalDatabaseSafetyError(
        "database URL is not recognizably local/test/eval; use EVAL_DATABASE_URL with a non-production target"
    )


async def create_eval_pool(database_url: str | None = None, **pool_kwargs: Any) -> Any:
    import asyncpg

    selected_url = database_url or os.getenv("EVAL_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
    allow_production = os.getenv("EVAL_ALLOW_PRODUCTION_DATABASE") == "1"
    ensure_safe_database_url(selected_url, allow_production=allow_production)
    return await asyncpg.create_pool(selected_url, **pool_kwargs)


@asynccontextmanager
async def scratch_schema(
    pool: Any,
    *,
    keep_db: bool = False,
    migrations_dir: Path = MIGRATIONS_DIR,
    schema: str | None = None,
) -> AsyncIterator[ScratchDatabase]:
    schema_name = schema or f"eval_{uuid4().hex}"
    _validate_schema_name(schema_name)
    await _execute(pool, f"CREATE SCHEMA {_quote_ident(schema_name)}")
    try:
        await apply_migrations(pool, schema_name, migrations_dir=migrations_dir)
        yield ScratchDatabase(pool=pool, schema=schema_name)
    finally:
        if not keep_db:
            await _execute(pool, f"DROP SCHEMA IF EXISTS {_quote_ident(schema_name)} CASCADE")


@asynccontextmanager
async def eval_database(
    database_url: str | None = None,
    *,
    keep_db: bool = False,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> AsyncIterator[ScratchDatabase]:
    pool = await create_eval_pool(database_url)
    try:
        async with scratch_schema(pool, keep_db=keep_db, migrations_dir=migrations_dir) as scratch:
            yield scratch
    finally:
        await pool.close()


async def apply_migrations(pool: Any, schema: str, *, migrations_dir: Path = MIGRATIONS_DIR) -> None:
    _validate_schema_name(schema)
    paths = sorted(
        path
        for path in migrations_dir.glob("*.sql")
        if path.name != "teardown.sql"
        and not path.name.endswith(".down.sql")
        and path.name not in DURABLE_MIGRATION_FILENAMES
    )
    async with pool.acquire() as connection:
        await connection.execute(f"SET search_path TO {_quote_ident(schema)}, public")
        for path in paths:
            sql = path.read_text(encoding="utf-8")
            await connection.execute(sql)
        await connection.execute("SET search_path TO public")


async def use_scratch_search_path(pool: Any, schema: str) -> None:
    _validate_schema_name(schema)
    await _execute(pool, f"SET search_path TO {_quote_ident(schema)}, public")


def _quote_ident(identifier: str) -> str:
    _validate_schema_name(identifier)
    return f'"{identifier}"'


def _validate_schema_name(identifier: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"invalid schema name: {identifier!r}")


async def _execute(pool: Any, sql: str) -> str:
    if hasattr(pool, "execute"):
        return await pool.execute(sql)
    async with pool.acquire() as connection:
        return await connection.execute(sql)
