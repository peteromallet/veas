import os
from decimal import Decimal

import pytest

from app.config import get_settings
from app.services.spend import is_under_cap, record_llm_cost


pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL unset",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _prepare_pool():
    asyncpg = pytest.importorskip("asyncpg")
    pool = await asyncpg.create_pool(os.environ["TEST_DATABASE_URL"])
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_spend_log (
            provider text NOT NULL,
            day date NOT NULL,
            total_usd numeric(10,4) NOT NULL DEFAULT 0,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (provider, day)
        )
        """
    )
    await pool.execute("DELETE FROM llm_spend_log WHERE provider = ANY($1::text[])", ["text", "vision"])
    return pool


@pytest.mark.anyio
async def test_record_llm_cost_and_cap_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEXT_LLM_DAILY_CAP_USD", "1.00")
    get_settings.cache_clear()
    pool = await _prepare_pool()
    try:
        assert await is_under_cap(pool, "text")
        await record_llm_cost(pool, "text", Decimal("0.60"))
        await record_llm_cost(pool, "text", Decimal("0.39"))
        assert await pool.fetchval(
            "SELECT total_usd FROM llm_spend_log WHERE provider = $1 AND day = CURRENT_DATE",
            "text",
        ) == Decimal("0.9900")
        assert await is_under_cap(pool, "text")
        await record_llm_cost(pool, "text", Decimal("0.01"))
        assert not await is_under_cap(pool, "text")
    finally:
        await pool.execute("DELETE FROM llm_spend_log WHERE provider = $1", "text")
        await pool.close()
        get_settings.cache_clear()


@pytest.mark.anyio
async def test_is_under_cap_maps_provider_to_matching_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISION_DAILY_CAP_USD", "0.50")
    get_settings.cache_clear()
    pool = await _prepare_pool()
    try:
        await record_llm_cost(pool, "vision", Decimal("0.49"))
        assert await is_under_cap(pool, "vision")
        await record_llm_cost(pool, "vision", Decimal("0.01"))
        assert not await is_under_cap(pool, "vision")
    finally:
        await pool.execute("DELETE FROM llm_spend_log WHERE provider = $1", "vision")
        await pool.close()
        get_settings.cache_clear()
