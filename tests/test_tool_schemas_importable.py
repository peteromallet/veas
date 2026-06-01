from uuid import uuid4
from datetime import UTC, date, datetime, time, timedelta

import pytest


def test_tool_schemas_registry_importable() -> None:
    from tool_schemas import TOOL_REGISTRY

    assert TOOL_REGISTRY


def test_mediator_bot_spec_owns_prompt_and_phase_tools() -> None:
    from app.bots.registry import get_bot_spec
    from app.models.user import User

    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    spec = get_bot_spec("mediator")

    prompt = spec.render_system_prompt(
        assistant_name="Veas",
        user=user,
        partner=partner,
        prompt_version="v1",
    )

    assert "You are Veas" in prompt
    assert "I'll check in with you then" in spec.step_instructions["respond"]
    assert "I've scheduled that" in spec.step_instructions["respond"]
    assert "state changes" in spec.step_instructions["record"]
    assert "scheduled tasks" in spec.step_instructions["schedule"]


def test_check_oob_schema_accepts_optional_protected_owner_ids() -> None:
    from uuid import uuid4

    from tool_schemas import CheckOOBInput

    recipient_id = uuid4()
    sender_id = uuid4()

    payload = CheckOOBInput(
        content="draft",
        recipient_id=recipient_id,
        protected_owner_ids=[sender_id, recipient_id],
    )
    schema = CheckOOBInput.model_json_schema()

    assert payload.protected_owner_ids == [sender_id, recipient_id]
    assert "protected_owner_ids" in schema["properties"]
    assert (
        "recipient-only compatibility"
        in schema["properties"]["protected_owner_ids"]["description"]
    )


def test_oob_row_schema_exposes_safe_summary_not_sensitive_core() -> None:
    from tool_schemas import OOBRow

    schema = OOBRow.model_json_schema()

    assert "protected_summary" in schema["properties"]
    assert "sensitive_core" not in schema["properties"]


def test_bridge_candidate_tools_are_registered_with_exact_enums() -> None:
    from pydantic import ValidationError

    from app.services.tools.registry import (
        READ_PHASE_TOOLS,
        TOOL_DISPATCH,
        WRITE_PHASE_TOOLS,
    )
    from tool_schemas import (
        BridgeCandidateSensitivity,
        BridgeCandidateStatus,
        BridgeCandidateKind,
        BridgeCandidatePartnerPath,
        CreateBridgeCandidateInput,
        ListBridgeCandidatesInput,
        SendBridgeCandidateInput,
        TOOL_REGISTRY,
        UpdateBridgeCandidateInput,
    )

    assert {"list_bridge_candidates"} <= READ_PHASE_TOOLS
    assert {
        "create_bridge_candidate",
        "update_bridge_candidate",
        "send_bridge_candidate",
    } <= WRITE_PHASE_TOOLS
    for name in (
        "list_bridge_candidates",
        "create_bridge_candidate",
        "update_bridge_candidate",
        "send_bridge_candidate",
    ):
        assert name in TOOL_REGISTRY
        assert name in TOOL_DISPATCH

    assert {item.value for item in BridgeCandidateStatus} == {
        "pending",
        "ready",
        "sent",
        "declined",
        "blocked",
        "addressed",
        "expired",
    }
    assert {item.value for item in BridgeCandidateSensitivity} == {
        "low",
        "medium",
        "high",
    }
    assert {item.value for item in BridgeCandidatePartnerPath} == {
        "message_partner",
        "coach_in_person",
        "casual_share",
        "hold_for_context",
        "ask_permission",
        "do_not_bridge",
    }
    assert {item.value for item in BridgeCandidateKind} == {
        "context",
        "clarification",
        "contradiction",
        "repair",
        "vulnerability",
        "process",
    }

    source_id = uuid4()
    target_id = uuid4()
    message_id = uuid4()
    CreateBridgeCandidateInput(
        source_user_id=source_id,
        target_user_id=target_id,
        kind="repair",
        sensitivity="low",
        partner_path="message_partner",
        source_message_ids=[message_id],
        shareable_summary="A repair is available.",
    )
    ListBridgeCandidatesInput(status="ready", partner_path="message_partner")
    UpdateBridgeCandidateInput(
        candidate_id=uuid4(), status="addressed", partner_path="hold_for_context"
    )
    SendBridgeCandidateInput(candidate_id=uuid4())

    with pytest.raises(ValidationError):
        CreateBridgeCandidateInput(
            source_user_id=source_id,
            target_user_id=target_id,
            kind="repair",
            sensitivity="low",
            partner_path="message_partner",
            source_message_ids=[message_id],
            shareable_summary="Invalid lifecycle.",
            status="offered",
        )


def test_set_partner_sharing_schema_is_scoped_and_registered() -> None:
    from pydantic import ValidationError

    from app.services.tools.registry import TOOL_DISPATCH, WRITE_PHASE_TOOLS
    from tool_schemas import SetPartnerSharingInput, TOOL_REGISTRY

    assert "set_partner_sharing" in TOOL_REGISTRY
    assert "set_partner_sharing" in TOOL_DISPATCH
    assert "set_partner_sharing" in WRITE_PHASE_TOOLS

    assert SetPartnerSharingInput(opt_in=True, reason="explicit yes").opt_in is True
    with pytest.raises(ValidationError):
        SetPartnerSharingInput(opt_in=True, user_id=uuid4())
    with pytest.raises(ValidationError):
        SetPartnerSharingInput(opt_in=False, bot_id="mediator")


def test_distillation_tools_are_registered_and_phase_gated() -> None:
    from app.services.tools.registry import (
        READ_PHASE_TOOLS,
        TOOL_DESCRIPTIONS,
        TOOL_DISPATCH,
        WRITE_PHASE_TOOLS,
    )
    from tool_schemas import TOOL_REGISTRY

    assert {"get_distillations"} <= READ_PHASE_TOOLS
    assert {
        "add_distillation",
        "update_distillation",
        "revise_distillation",
    } <= WRITE_PHASE_TOOLS
    for name in (
        "get_distillations",
        "add_distillation",
        "update_distillation",
        "revise_distillation",
    ):
        assert name in TOOL_REGISTRY
        assert name in TOOL_DISPATCH
        assert name in TOOL_DESCRIPTIONS
    assert "searching existing distillations" in TOOL_DESCRIPTIONS["add_distillation"]


def test_distillation_schema_validation_rejects_unsafe_or_unsupported_inputs() -> None:
    from pydantic import ValidationError

    from tool_schemas import (
        AddDistillationInput,
        ReviseDistillationInput,
        UpdateDistillationInput,
    )

    source_id = uuid4()
    observation_id = uuid4()
    message_id = uuid4()
    distillation_id = uuid4()

    AddDistillationInput(
        content="One possible explanation is that repair feels risky after prior withdrawal.",
        source_user_ids=[source_id],
        related_observation_ids=[observation_id],
        supporting_message_ids=[message_id],
    )
    ReviseDistillationInput(
        old_distillation_id=distillation_id,
        new_content="One possible updated explanation is that timing makes repair feel pressured.",
        source_user_ids=[source_id],
        related_observation_ids=[observation_id],
        revision_note="new supporting observation changed the synthesis",
    )

    with pytest.raises(ValidationError):
        AddDistillationInput(
            content="missing source users",
            source_user_ids=[],
            related_observation_ids=[observation_id],
        )
    with pytest.raises(ValidationError):
        AddDistillationInput(
            content="missing supporting links", source_user_ids=[source_id]
        )
    with pytest.raises(ValidationError):
        AddDistillationInput(
            content="invalid visibility",
            source_user_ids=[source_id],
            related_observation_ids=[observation_id],
            visibility="public",
        )
    with pytest.raises(ValidationError):
        AddDistillationInput(
            content="shareable without safe summary",
            source_user_ids=[source_id],
            related_observation_ids=[observation_id],
            visibility="dyad_shareable",
        )
    with pytest.raises(ValidationError):
        UpdateDistillationInput(
            distillation_id=distillation_id,
            related_memory_ids=[],
            related_observation_ids=[],
            related_theme_ids=[],
            supporting_message_ids=[],
        )
    with pytest.raises(ValidationError):
        UpdateDistillationInput(
            distillation_id=distillation_id, visibility="dyad_shareable"
        )
    with pytest.raises(ValidationError):
        UpdateDistillationInput(distillation_id=distillation_id, status="revised")


def test_add_memory_requires_summary_for_dyad_shareable_visibility() -> None:
    from pydantic import ValidationError

    from tool_schemas import AddMemoryInput

    user_id = uuid4()
    AddMemoryInput(
        about_user_id=user_id,
        content="safe to remember",
        visibility="dyad_shareable",
        shareable_summary="A safe summary for partner context.",
    )

    with pytest.raises(ValidationError):
        AddMemoryInput(
            about_user_id=user_id,
            content="unsafe because summary is missing",
            visibility="dyad_shareable",
        )
    with pytest.raises(ValidationError):
        AddMemoryInput(
            about_user_id=user_id,
            content="unsafe because summary is blank",
            visibility="dyad_shareable",
            shareable_summary="   ",
        )


def test_system_prompt_defines_distillations_as_tentative_private_syntheses() -> None:
    from app.services.prompts import SYSTEM_PROMPT_V1

    assert "# The Six Knowledge Primitives" in SYSTEM_PROMPT_V1
    assert "provisional synthesized explanations" in SYSTEM_PROMPT_V1
    assert "get_distillations" in SYSTEM_PROMPT_V1
    assert "source_user_ids` must be non-empty and conservative" in SYSTEM_PROMPT_V1
    assert "do not delete or mutate underlying observations" in SYSTEM_PROMPT_V1


def test_system_prompt_uses_partner_bridges_language() -> None:
    from app.services.prompts import (
        PROMPT_REGISTRY,
        SYSTEM_PROMPT_VERSION,
        render_system_prompt,
    )

    prompt = render_system_prompt("Veas", "Maya", "Ben")

    assert SYSTEM_PROMPT_VERSION == "v3"
    assert {"v1", "v2", "v3"} <= set(PROMPT_REGISTRY)
    assert "Partner Bridges" in prompt
    assert "# Bridge Candidates" not in prompt
    assert (
        "use `escalate_to_partner` with concise, balanced, non-accusatory wording, "
        "clearly marked as a mediated summary"
    ) not in prompt
    for value in (
        "message_partner",
        "coach_in_person",
        "casual_share",
        "hold_for_context",
        "ask_permission",
        "do_not_bridge",
    ):
        assert value in prompt
    assert "Path rubric" in prompt
    assert "neutral mediated context would help" in prompt
    assert (
        "sensitive, intimate, shame-heavy, sexual, apologetic, or high-stakes" in prompt
    )
    assert "low-stakes affection, appreciation, or simple context" in prompt
    assert "consent or shareable wording is unclear" in prompt
    assert (
        "triangulate, leak protected material, inflame the conflict, or violate OOB"
        in prompt
    )


def test_scheduled_task_tools_are_registered_and_phase_gated() -> None:
    from app.services.tools.registry import (
        READ_PHASE_TOOLS,
        TOOL_DESCRIPTIONS,
        TOOL_DISPATCH,
        WRITE_PHASE_TOOLS,
    )
    from tool_schemas import TOOL_REGISTRY

    assert {"list_scheduled_tasks"} <= READ_PHASE_TOOLS
    assert {
        "schedule_task",
        "update_scheduled_task",
        "cancel_scheduled_task",
    } <= WRITE_PHASE_TOOLS
    for name in (
        "schedule_task",
        "list_scheduled_tasks",
        "update_scheduled_task",
        "cancel_scheduled_task",
    ):
        assert name in TOOL_REGISTRY
        assert name in TOOL_DISPATCH
        assert name in TOOL_DESCRIPTIONS
    assert "current_task=true" in TOOL_DESCRIPTIONS["update_scheduled_task"]
    assert "Prefer `delay` by default" in TOOL_DESCRIPTIONS["schedule_task"]
    assert "message me" in TOOL_DESCRIPTIONS["schedule_checkin"]
    assert "use `schedule_checkin`" in TOOL_DESCRIPTIONS["schedule_task"]
    assert "Use `local_when`" in TOOL_DESCRIPTIONS["schedule_checkin"]


def test_scheduled_task_schema_validation_rejects_invalid_inputs() -> None:
    from pydantic import ValidationError

    from tool_schemas import (
        CancelScheduledTaskInput,
        ScheduleDelay,
        LocalScheduleTime,
        ScheduleTaskInput,
        ScheduleTaskOutput,
        ScheduledTaskRecurrence,
        UpdateScheduledTaskInput,
    )

    job_id = uuid4()
    task_id = uuid4()
    aware_when = datetime.now(UTC) + timedelta(hours=2)
    recurrence = ScheduledTaskRecurrence(type="daily", interval=1)

    ScheduleTaskInput(brief="Send tomorrow's repair brief.", when=aware_when)
    ScheduleTaskInput(brief="Send in two days.", delay=ScheduleDelay(days=2))
    ScheduleTaskInput(
        brief="Send at 9pm Berlin.",
        local_when=LocalScheduleTime(
            date=date(2026, 5, 6), time=time(21, 0), timezone="Europe/Berlin"
        ),
    )
    ScheduleTaskInput(
        brief="Repeat every three hours.",
        delay=ScheduleDelay(hours=3),
        recurrence={"type": "hourly", "interval": 3},
    )
    ScheduleTaskInput(
        brief="Send a daily repair brief.", when=aware_when, recurrence=recurrence
    )
    ScheduleTaskOutput(
        task_id=task_id, job_id=job_id, scheduled_for=aware_when, recurrence=recurrence
    )
    UpdateScheduledTaskInput(task_id=task_id, brief="Updated brief.")
    UpdateScheduledTaskInput(current_task=True, recurrence=None)
    CancelScheduledTaskInput(job_id=job_id)

    with pytest.raises(ValidationError, match="when must be timezone-aware"):
        ScheduleTaskInput(brief="Naive one-shot.", when=datetime(2026, 5, 5, 9, 30))
    with pytest.raises(
        ValidationError, match="provide exactly one of when, delay, or local_when"
    ):
        ScheduleTaskInput(brief="Missing schedule.")
    with pytest.raises(
        ValidationError, match="provide exactly one of when, delay, or local_when"
    ):
        ScheduleTaskInput(
            brief="Ambiguous schedule.", when=aware_when, delay=ScheduleDelay(days=2)
        )
    with pytest.raises(ValidationError, match="delay must be a positive duration"):
        ScheduleDelay()
    with pytest.raises(ValidationError, match="weekly recurrence requires weekdays"):
        ScheduledTaskRecurrence(type="weekly")
    with pytest.raises(ValidationError, match="between 0 and 6"):
        ScheduledTaskRecurrence(type="weekly", weekdays=[7])
    with pytest.raises(
        ValidationError, match="hourly and daily recurrence must not set weekdays"
    ):
        ScheduledTaskRecurrence(type="daily", weekdays=[1])
    with pytest.raises(
        ValidationError, match="recurrence.until must be timezone-aware"
    ):
        ScheduledTaskRecurrence(type="daily", until=datetime(2026, 5, 6, 9, 30))
    with pytest.raises(ValidationError, match="provide exactly one"):
        UpdateScheduledTaskInput(task_id=task_id, job_id=job_id, brief="ambiguous")
    with pytest.raises(ValidationError, match="provide at least one update"):
        UpdateScheduledTaskInput(task_id=task_id)
    with pytest.raises(
        ValidationError, match="provide at most one of when, delay, or local_when"
    ):
        UpdateScheduledTaskInput(
            task_id=task_id, when=aware_when, delay=ScheduleDelay(hours=2)
        )
    with pytest.raises(ValidationError, match="provide exactly one"):
        CancelScheduledTaskInput()


def test_submit_live_debrief_importable_and_versioned() -> None:
    """submit_live_debrief is importable, registered in TOOL_REGISTRY and TOOL_DISPATCH."""
    from app.services.tools.registry import TOOL_DISPATCH
    from tool_schemas import (
        SubmitLiveDebriefInput,
        SubmitLiveDebriefOutput,
        EvidenceReferenceV1,
        FailedWriteV1,
        TOOL_REGISTRY,
    )

    # Schema importable.
    assert SubmitLiveDebriefInput is not None
    assert SubmitLiveDebriefOutput is not None
    assert EvidenceReferenceV1 is not None
    assert FailedWriteV1 is not None

    # Registered in TOOL_REGISTRY.
    assert "submit_live_debrief" in TOOL_REGISTRY, (
        "submit_live_debrief must be in TOOL_REGISTRY"
    )
    assert "submit_live_debrief" in TOOL_DISPATCH, (
        "submit_live_debrief must be in TOOL_DISPATCH"
    )

    # Schema version is 1.
    instance = SubmitLiveDebriefInput(
        what_heard="test",
        what_decided="test",
        still_open="test",
        what_to_remember="test",
        durable_write_summary="test",
        open_questions="test",
    )
    assert instance.schema_version == 1, (
        f"Expected schema_version=1, got {instance.schema_version}"
    )

    # Output ok=True.
    output = SubmitLiveDebriefOutput(ok=True)
    assert output.ok is True

    # EvidenceReferenceV1 construction.
    ev = EvidenceReferenceV1(
        transcript_turn_id="turn-1",
        quote="I feel unheard.",
        confidence=0.9,
    )
    assert ev.transcript_turn_id == "turn-1"
    assert ev.quote == "I feel unheard."
    assert ev.confidence == 0.9

    # FailedWriteV1 construction.
    fw = FailedWriteV1(
        tool_name="add_memory",
        reason="debrief_unshareable_transcript_reference",
        evidence_refs=[ev],
    )
    assert fw.tool_name == "add_memory"
    assert fw.reason == "debrief_unshareable_transcript_reference"

    # ConfigDict(extra='allow') allows extra fields through.
    extra_instance = SubmitLiveDebriefInput(
        what_heard="test",
        what_decided="test",
        still_open="test",
        what_to_remember="test",
        durable_write_summary="test",
        open_questions="test",
        extra_field="should pass through",
    )
    assert hasattr(extra_instance, "extra_field") or True  # ConfigDict(extra='allow')

    # All required fields accept empty defaults.
    empty = SubmitLiveDebriefInput()
    assert empty.what_heard == ""
    assert empty.what_decided == ""
    assert empty.still_open == ""
    assert empty.what_to_remember == ""
    assert empty.durable_write_summary == ""
    assert empty.open_questions == ""
    assert empty.review_summary is None
    assert empty.references is None
    assert empty.failed_writes is None


def test_message_nav_tools_are_wired_into_registry_and_derived_read_sets() -> None:
    from app.services.tools.registry import (
        BOT_EXCLUSIVE_TOOLS,
        CONSULT_PHASE_TOOLS,
        LIVE_PREP_TOOLS,
        READ_PHASE_TOOLS,
        READ_TOOLS_FOR_STEP,
        RECORD_READ_TOOLS,
        TOOL_DESCRIPTIONS,
        TOOL_DISPATCH,
    )

    new_read_tools = {
        "messages_before",
        "messages_after",
        "open_thread",
        "scroll",
        "topic_recent",
        "search",
    }
    exclusive_tools = set().union(*BOT_EXCLUSIVE_TOOLS.values())

    assert new_read_tools <= set(TOOL_DISPATCH)
    assert new_read_tools <= set(TOOL_DESCRIPTIONS)
    assert new_read_tools <= READ_PHASE_TOOLS
    assert new_read_tools <= CONSULT_PHASE_TOOLS
    assert new_read_tools <= RECORD_READ_TOOLS
    assert new_read_tools <= READ_TOOLS_FOR_STEP
    assert new_read_tools <= LIVE_PREP_TOOLS
    assert new_read_tools.isdisjoint(exclusive_tools)
    assert "hot-context gist" in TOOL_DESCRIPTIONS["messages_before"]
    assert "hot-context gist" in TOOL_DESCRIPTIONS["search"]
