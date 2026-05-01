from __future__ import annotations

from pathlib import Path

import pytest

from evals.scenario import ScenarioError, load_scenario, load_scenarios


def write_scenario(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


VALID_SCENARIO = """---
name: worked-example-replay
description: Canonical worked example replay.
tags: [worked-example, dedup, oob]
setup:
  users:
    - name: Maya
      phone: "+15555550100"
  observations:
    - id: obs_4d2
      content: Ben goes quiet around deadlines.
inbound:
  text: she didn't ask how my day went tonight, again.
expectations:
  must_call_tools: [get_observations, update_observation]
  must_not_call_tools: [log_observation, escalate_to_partner]
  must_write_primitives:
    - kind: observation
      operation: update
      content_matches: "ask.*day|deadline"
      significance_min: 3
      significance_max: 5
    - kind: watch_item
      count: 1
      content_matches: "disconnection|asked.*day"
  outbound_assertions:
    - names the recurring pattern without pathologizing
    - asks Maya whether she wants to vent or process
  must_pass_oob: true
  expected_charge: charged
---
## Notes

Synthetic scenario body.
"""


def test_load_scenario_parses_valid_front_matter(tmp_path: Path) -> None:
    path = write_scenario(tmp_path, "worked.md", VALID_SCENARIO)

    scenario = load_scenario(path)

    assert scenario.name == "worked-example-replay"
    assert scenario.tags == ["worked-example", "dedup", "oob"]
    assert scenario.setup["users"][0]["name"] == "Maya"
    assert scenario.inbound[0].text == "she didn't ask how my day went tonight, again."
    assert scenario.expectations.must_call_tools == ["get_observations", "update_observation"]
    assert scenario.expectations.must_not_call_tools == ["log_observation", "escalate_to_partner"]
    assert scenario.expectations.must_pass_oob is True
    assert scenario.expectations.expected_charge == "charged"
    assert scenario.expectations.must_write_primitives[0].kind == "observation"
    assert scenario.expectations.must_write_primitives[0].operation == "update"
    assert scenario.body.startswith("## Notes")


def test_load_scenarios_filters_by_name_and_tag(tmp_path: Path) -> None:
    write_scenario(tmp_path, "worked.md", VALID_SCENARIO)
    write_scenario(
        tmp_path,
        "routine.md",
        VALID_SCENARIO.replace("worked-example-replay", "routine-check").replace(
            "tags: [worked-example, dedup, oob]", "tags: [charge]"
        ),
    )

    assert [s.name for s in load_scenarios(tmp_path, scenario_name="routine-check")] == ["routine-check"]
    assert [s.name for s in load_scenarios(tmp_path, tag="dedup")] == ["worked-example-replay"]


def test_load_scenario_rejects_malformed_expectations(tmp_path: Path) -> None:
    path = write_scenario(
        tmp_path,
        "bad.md",
        VALID_SCENARIO.replace("must_call_tools: [get_observations, update_observation]", "must_call_tools: get_observations"),
    )

    with pytest.raises(ScenarioError, match="expectations.must_call_tools must be a list"):
        load_scenario(path)


def test_load_scenario_rejects_unknown_charge(tmp_path: Path) -> None:
    path = write_scenario(tmp_path, "bad-charge.md", VALID_SCENARIO.replace("expected_charge: charged", "expected_charge: tense"))

    with pytest.raises(ScenarioError, match="expectations.expected_charge must be one of"):
        load_scenario(path)


def test_load_scenario_rejects_conflicting_oob_expectations(tmp_path: Path) -> None:
    path = write_scenario(
        tmp_path,
        "bad-oob.md",
        VALID_SCENARIO.replace("must_pass_oob: true", "must_pass_oob: true\n  expected_oob: block"),
    )

    with pytest.raises(ScenarioError, match="must_pass_oob conflicts"):
        load_scenario(path)
