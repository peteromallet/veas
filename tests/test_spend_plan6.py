from decimal import Decimal

import pytest

from app.config import get_settings
from app.services.spend import record_llm_cost


pytestmark = pytest.mark.anyio


async def test_record_llm_cost_warns_once_at_80_percent(fake_pool, monkeypatch, caplog) -> None:
    monkeypatch.setenv("TEXT_LLM_DAILY_CAP_USD", "1.00")
    get_settings.cache_clear()

    await record_llm_cost(fake_pool, "text", Decimal("0.79"))
    assert "crossed 80%" not in caplog.text

    await record_llm_cost(fake_pool, "text", Decimal("0.01"))
    assert fake_pool.llm_spend_log["text"]["warned_80_at"] is not None
    assert caplog.text.count("crossed 80%") == 1

    await record_llm_cost(fake_pool, "text", Decimal("0.05"))
    assert caplog.text.count("crossed 80%") == 1
    get_settings.cache_clear()
