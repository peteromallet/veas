from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from evals.execution import EvalTurnExecution, FakeWhatsAppSend, OobCheckRecord
from evals.factories import capture_scenario_turn, seed_scenario
from evals.scenario import InboundMessage, Scenario, ScenarioExpectations
from evals.state import diff_snapshots, snapshot_state

pytestmark = pytest.mark.anyio


def _scenario(
    *,
    name: str = "factory-smoke",
    tags: list[str] | None = None,
    setup: dict | None = None,
    inbound: list[InboundMessage] | None = None,
    expectations: ScenarioExpectations | None = None,
) -> Scenario:
    return Scenario(
        name=name,
        description="Factory smoke scenario",
        tags=tags or ["factory"],
        setup=setup or {},
        inbound=inbound or [InboundMessage("I felt alone after dinner.")],
        expectations=expectations or ScenarioExpectations(),
        path=Path(f"{name}.md"),
    )


async def test_seed_scenario_creates_synthetic_state_and_direct_inbound(fake_pool) -> None:
    scenario = _scenario(
        setup={
            "users": [
                {"key": "maya", "name": "Maya", "phone": "15555550100", "style_notes": "Talks it out."},
                {"key": "ben", "name": "Ben", "phone": "15555550101"},
            ],
            "inbound_charge": "charged",
            "themes": [{"key": "repair", "title": "Repair timing", "description": "They recover slowly."}],
            "memories": [{"key": "trip", "about": "maya", "content": "They have a trip planned."}],
            "observations": [
                {
                    "key": "quiet",
                    "about": "maya",
                    "content": "Maya gets quiet after rushed apologies.",
                    "significance": 4,
                    "related_themes": ["repair"],
                }
            ],
            "distillations": [
                {
                    "key": "repair_explanation",
                    "content": "One possible explanation is that repair feels rushed.",
                    "source_users": ["maya"],
                    "related_observations": ["quiet"],
                    "related_themes": ["repair"],
                    "shareable_summary": "Repair may feel rushed.",
                    "visibility": "dyad_shareable",
                }
            ],
            "watch_items": [{"key": "followup", "owner": "maya", "content": "Ask whether the talk happened."}],
            "scheduled_jobs": [{"key": "checkin", "user": "maya", "job_type": "checkin", "scheduled_for": {"in_hours": 4}}],
            "oob_entries": [
                {
                    "key": "oob",
                    "owner": "maya",
                    "sensitive_core": "Do not mention the private medical detail.",
                    "shareable_context": "health topic",
                    "severity": "hard",
                }
            ],
        },
        inbound=[InboundMessage("First note."), InboundMessage("Second note.")],
    )

    seed = await seed_scenario(fake_pool, scenario)
    snapshot = await snapshot_state(fake_pool)

    assert seed.user.name == "Maya"
    assert seed.partner.name == "Ben"
    assert len(seed.inbound_message_ids) == 2
    assert seed.refs["repair"]
    assert seed.refs["quiet"]
    assert all(fake_pool.messages[message_id]["charge"] == "charged" for message_id in seed.inbound_message_ids)
    assert snapshot.tables["themes"][str(seed.refs["repair"])]["title"] == "Repair timing"
    assert snapshot.tables["observations"][str(seed.refs["quiet"])]["significance"] == 4
    assert snapshot.tables["distillations"][str(seed.refs["repair_explanation"])]["shareable_summary"] == "Repair may feel rushed."
    assert snapshot.tables["out_of_bounds"][str(seed.refs["oob"])]["severity"] == "hard"
    assert snapshot.tables["scheduled_jobs"][str(seed.refs["checkin"])]["job_type"] == "checkin"


async def test_charge_scenario_inserts_inbound_through_process_inbound(fake_pool, app_env, monkeypatch) -> None:
    calls = []

    async def fake_classify_charge(pool, text):
        calls.append((pool, text))
        return SimpleNamespace(charge="crisis")

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    scenario = _scenario(
        name="charge-classification",
        tags=["charge"],
        inbound=[InboundMessage("I might hurt myself tonight.")],
    )

    seed = await seed_scenario(fake_pool, scenario)

    assert len(seed.inbound_message_ids) == 1
    assert calls == [(fake_pool, "I might hurt myself tonight.")]
    message = fake_pool.messages[seed.inbound_message_ids[0]]
    assert message["charge"] == "crisis"
    assert message["processing_state"] == "raw"


async def test_capture_scenario_turn_returns_diffs_outputs_oob_charge_and_cost(fake_pool, monkeypatch) -> None:
    scenario = _scenario(
        setup={
            "inbound_charge": "charged",
            "observations": [{"key": "obs", "content": "Maya withdraws when repair is delayed.", "significance": 4}],
        }
    )

    async def fake_run_eval_turn(pool, triggering_message_ids, user, *, prompt_version):
        observation_id = next(iter(pool.observations))
        pool.observations[observation_id]["content"] = "Maya withdraws when repair is delayed, then reaches back."
        watch_id = uuid4()
        pool.watch_items[watch_id] = {
            "id": watch_id,
            "owner_user_id": user.id,
            "content": "Check whether the repair talk happened.",
            "due_at": datetime.now(UTC),
            "related_theme_ids": [],
            "status": "open",
        }
        outbound_id = uuid4()
        pool.messages[outbound_id] = {
            "id": outbound_id,
            "direction": "outbound",
            "sender_id": None,
            "recipient_id": user.id,
            "content": "That sounds lonely.",
            "processing_state": "withheld",
            "sent_at": datetime.now(UTC),
            "charge": None,
            "deleted_at": None,
            "whatsapp_message_id": None,
        }
        review_id = uuid4()
        pool.withheld_outbound_reviews[review_id] = {
            "id": review_id,
            "recipient_id": user.id,
            "outbound_id": outbound_id,
            "original_content": "That sounds lonely.",
            "suggested_rewrite": "That sounds hard.",
            "reason": "firm OOB",
            "verdict": "rewrite",
            "checker_failed": False,
            "status": "pending",
            "created_at": datetime.now(UTC),
        }
        pool.tool_calls.append(
            {
                "turn_id": uuid4(),
                "tool_name": "update_observation",
                "arguments": {"observation_id": str(observation_id)},
                "result": {"id": str(observation_id)},
                "called_at": datetime.now(UTC),
                "duration_ms": 2,
            }
        )
        pool.llm_spend_log["text"] = Decimal("0.42")
        return EvalTurnExecution(
            tool_calls=[
                {
                    "tool_name": "update_observation",
                    "args": {"observation_id": str(observation_id)},
                    "result": {"id": str(observation_id)},
                    "phase": "write",
                    "duration_ms": 2,
                    "called_at": datetime.now(UTC).isoformat(),
                }
            ],
            whatsapp_sends=[FakeWhatsAppSend("text", user.phone, "That sounds lonely.", "eval-text-1")],
            oob_checks=[OobCheckRecord("That sounds lonely.", str(user.id), {"verdict": "rewrite"})],
        )

    monkeypatch.setattr("evals.factories.run_eval_turn", fake_run_eval_turn)

    capture = await capture_scenario_turn(fake_pool, scenario, prompt_version="v1")

    assert capture.outbound_text == "That sounds lonely."
    assert capture.oob_outcome == "rewrite"
    assert capture.classified_charges == {str(capture.seed.inbound_message_ids[0]): "charged"}
    assert capture.cost_delta_usd == "0.42"
    assert capture.persisted_tool_calls[0]["tool_name"] == "update_observation"
    assert capture.withheld_reviews[0]["verdict"] == "rewrite"
    assert capture.diff.tables["watch_items"].inserted[0]["content"] == "Check whether the repair talk happened."
    assert capture.diff.tables["observations"].updated[0]["content"].endswith("then reaches back.")
    assert capture.execution.whatsapp_sends[0].delivery_id == "eval-text-1"


async def test_snapshot_diff_reports_insert_update_delete_and_cost(fake_pool) -> None:
    before = await snapshot_state(fake_pool)
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": uuid4(),
        "content": "hello",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": None,
        "deleted_at": None,
    }
    fake_pool.llm_spend_log["text"] = Decimal("0.05")
    after = await snapshot_state(fake_pool)

    diff = diff_snapshots(before, after)

    assert diff.tables["messages"].inserted[0]["content"] == "hello"
    assert diff.cost_delta_usd == Decimal("0.05")
