from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml


Charge = Literal["routine", "notable", "charged", "crisis"]
OobOutcome = Literal["pass", "block", "rewrite"]
PrimitiveKind = Literal["memory", "theme", "watch_item", "observation", "distillation", "style_note", "oob_entry"]


class ScenarioError(ValueError):
    """Raised when a scenario file is malformed."""


@dataclass(frozen=True)
class InboundMessage:
    text: str
    media_type: str | None = None
    media_url: str | None = None
    media_duration_seconds: int | None = None


@dataclass(frozen=True)
class PrimitiveWriteExpectation:
    kind: PrimitiveKind
    count: int | None = None
    content_matches: str | None = None
    status: str | None = None
    significance_min: int | None = None
    significance_max: int | None = None
    operation: Literal["insert", "update", "supersede"] | None = None


@dataclass(frozen=True)
class ScenarioExpectations:
    must_call_tools: list[str] = field(default_factory=list)
    must_not_call_tools: list[str] = field(default_factory=list)
    must_write_primitives: list[PrimitiveWriteExpectation] = field(default_factory=list)
    outbound_assertions: list[str] = field(default_factory=list)
    must_pass_oob: bool | None = None
    expected_oob: OobOutcome | None = None
    expected_charge: Charge | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    tags: list[str]
    setup: dict[str, Any]
    inbound: list[InboundMessage]
    expectations: ScenarioExpectations
    path: Path
    body: str = ""


def load_scenarios(
    scenarios_dir: Path,
    *,
    scenario_name: str | None = None,
    tag: str | None = None,
) -> list[Scenario]:
    if not scenarios_dir.exists():
        if scenario_name or tag:
            raise ScenarioError(f"scenario directory does not exist: {scenarios_dir}")
        return []
    paths = sorted(scenarios_dir.glob("*.md"))
    scenarios = [load_scenario(path) for path in paths]
    if scenario_name:
        scenarios = [scenario for scenario in scenarios if scenario.name == scenario_name]
        if not scenarios:
            raise ScenarioError(f"scenario not found: {scenario_name}")
    if tag:
        scenarios = [scenario for scenario in scenarios if tag in scenario.tags]
        if not scenarios:
            raise ScenarioError(f"no scenarios found with tag: {tag}")
    return scenarios


def load_scenario(path: Path) -> Scenario:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioError(f"{path}: cannot read scenario: {exc}") from exc
    front_matter, body = _split_front_matter(text, path)
    data = _load_yaml(front_matter, path)
    return _parse_scenario(data, body, path)


def _split_front_matter(text: str, path: Path) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise ScenarioError(f"{path}: scenario must start with YAML front matter delimiter '---'")
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        raise ScenarioError(f"{path}: scenario is missing closing YAML front matter delimiter")
    front_matter = parts[0][4:]
    body = parts[1]
    if body.startswith("\n"):
        body = body[1:]
    return front_matter, body


def _load_yaml(front_matter: str, path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(front_matter)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: invalid YAML front matter: {exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"{path}: YAML front matter must be a mapping")
    return data


def _parse_scenario(data: dict[str, Any], body: str, path: Path) -> Scenario:
    name = _required_str(data, "name", path)
    description = _required_str(data, "description", path)
    tags = _str_list(data.get("tags", []), "tags", path)
    setup = data.get("setup", {})
    if not isinstance(setup, dict):
        raise ScenarioError(f"{path}: setup must be a mapping")
    inbound = _parse_inbound(data.get("inbound"), path)
    expectations = _parse_expectations(data.get("expectations"), path)
    return Scenario(
        name=name,
        description=description,
        tags=tags,
        setup=setup,
        inbound=inbound,
        expectations=expectations,
        path=path,
        body=body,
    )


def _parse_inbound(value: Any, path: Path) -> list[InboundMessage]:
    if value is None:
        raise ScenarioError(f"{path}: inbound is required")
    values = value if isinstance(value, list) else [value]
    messages: list[InboundMessage] = []
    for index, item in enumerate(values):
        label = f"inbound[{index}]"
        if isinstance(item, str):
            messages.append(InboundMessage(text=item))
            continue
        if not isinstance(item, dict):
            raise ScenarioError(f"{path}: {label} must be a string or mapping")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ScenarioError(f"{path}: {label}.text must be a non-empty string")
        duration = item.get("media_duration_seconds")
        if duration is not None and (not isinstance(duration, int) or duration < 0):
            raise ScenarioError(f"{path}: {label}.media_duration_seconds must be a non-negative integer")
        messages.append(
            InboundMessage(
                text=text,
                media_type=_optional_str(item, "media_type", path, label),
                media_url=_optional_str(item, "media_url", path, label),
                media_duration_seconds=duration,
            )
        )
    return messages


def _parse_expectations(value: Any, path: Path) -> ScenarioExpectations:
    if not isinstance(value, dict):
        raise ScenarioError(f"{path}: expectations must be a mapping")
    must_call_tools = _str_list(value.get("must_call_tools", []), "expectations.must_call_tools", path)
    must_not_call_tools = _str_list(
        value.get("must_not_call_tools", []), "expectations.must_not_call_tools", path
    )
    outbound_assertions = _str_list(
        value.get("outbound_assertions", []), "expectations.outbound_assertions", path
    )
    must_pass_oob = value.get("must_pass_oob")
    if must_pass_oob is not None and not isinstance(must_pass_oob, bool):
        raise ScenarioError(f"{path}: expectations.must_pass_oob must be a boolean")
    expected_oob = value.get("expected_oob")
    if expected_oob is not None:
        _expect_choice(expected_oob, {"pass", "block", "rewrite"}, "expectations.expected_oob", path)
    if must_pass_oob is not None and expected_oob is not None:
        implied = "pass" if must_pass_oob else "block"
        if expected_oob != implied and not (must_pass_oob is False and expected_oob == "rewrite"):
            raise ScenarioError(f"{path}: expectations.must_pass_oob conflicts with expected_oob")
    expected_charge = value.get("expected_charge")
    if expected_charge is not None:
        _expect_choice(expected_charge, {"routine", "notable", "charged", "crisis"}, "expectations.expected_charge", path)
    writes = _parse_primitive_writes(value.get("must_write_primitives", []), path)
    return ScenarioExpectations(
        must_call_tools=must_call_tools,
        must_not_call_tools=must_not_call_tools,
        must_write_primitives=writes,
        outbound_assertions=outbound_assertions,
        must_pass_oob=must_pass_oob,
        expected_oob=expected_oob,
        expected_charge=expected_charge,
    )


def _parse_primitive_writes(value: Any, path: Path) -> list[PrimitiveWriteExpectation]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ScenarioError(f"{path}: expectations.must_write_primitives must be a list")
    writes: list[PrimitiveWriteExpectation] = []
    for index, item in enumerate(value):
        label = f"expectations.must_write_primitives[{index}]"
        if not isinstance(item, dict):
            raise ScenarioError(f"{path}: {label} must be a mapping")
        kind = item.get("kind")
        _expect_choice(
            kind,
            {"memory", "theme", "watch_item", "observation", "distillation", "style_note", "oob_entry"},
            f"{label}.kind",
            path,
        )
        operation = item.get("operation")
        if operation is not None:
            _expect_choice(operation, {"insert", "update", "supersede"}, f"{label}.operation", path)
        writes.append(
            PrimitiveWriteExpectation(
                kind=kind,
                count=_optional_non_negative_int(item, "count", path, label),
                content_matches=_optional_str(item, "content_matches", path, label),
                status=_optional_str(item, "status", path, label),
                significance_min=_optional_score(item, "significance_min", path, label),
                significance_max=_optional_score(item, "significance_max", path, label),
                operation=operation,
            )
        )
    return writes


def _required_str(data: dict[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ScenarioError(f"{path}: {key} must be a non-empty string")
    return value


def _optional_str(data: dict[str, Any], key: str, path: Path, label: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScenarioError(f"{path}: {label}.{key} must be a string")
    return value


def _str_list(value: Any, key: str, path: Path) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ScenarioError(f"{path}: {key} must be a list of non-empty strings")
    return value


def _optional_non_negative_int(data: dict[str, Any], key: str, path: Path, label: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or value < 0:
        raise ScenarioError(f"{path}: {label}.{key} must be a non-negative integer")
    return value


def _optional_score(data: dict[str, Any], key: str, path: Path, label: str) -> int | None:
    value = _optional_non_negative_int(data, key, path, label)
    if value is not None and not 1 <= value <= 5:
        raise ScenarioError(f"{path}: {label}.{key} must be between 1 and 5")
    return value


def _expect_choice(value: Any, choices: set[str], key: str, path: Path) -> None:
    if value not in choices:
        expected = ", ".join(sorted(choices))
        raise ScenarioError(f"{path}: {key} must be one of: {expected}")
