from __future__ import annotations

from pathlib import Path

from evals.scenario import load_scenarios


SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "evals" / "scenarios"


def test_committed_scenario_corpus_covers_required_classes() -> None:
    scenarios = load_scenarios(SCENARIOS_DIR)

    assert len(scenarios) >= 15
    names = {scenario.name for scenario in scenarios}
    tags = {tag for scenario in scenarios for tag in scenario.tags}

    assert "worked-example-replay" in names
    assert "dedup-repeated-observation" in names
    assert {"oob", "block", "rewrite", "crisis", "non-crisis", "redirection", "theme-discipline"} <= tags
    assert {"stance", "significance", "charge", "checkin", "pause"} <= tags
    assert any(scenario.expectations.expected_charge == "routine" for scenario in scenarios)
    assert any(scenario.expectations.expected_charge == "notable" for scenario in scenarios)
    assert any(scenario.expectations.expected_charge == "charged" for scenario in scenarios)
    assert any(scenario.expectations.expected_charge == "crisis" for scenario in scenarios)
    assert any("update_observation" in scenario.expectations.must_call_tools for scenario in scenarios)
    assert any(scenario.expectations.expected_oob == "block" for scenario in scenarios)
    assert any(scenario.expectations.expected_oob == "rewrite" for scenario in scenarios)


def test_charge_scenarios_require_classifier_instead_of_seeded_charge() -> None:
    scenarios = [scenario for scenario in load_scenarios(SCENARIOS_DIR) if "charge" in scenario.tags]

    assert scenarios
    assert all(scenario.setup.get("classify_inbound") is True for scenario in scenarios)
    assert all("inbound_charge" not in scenario.setup for scenario in scenarios)
