"""D25 LIVE runner registry: expectations, scenario pairs and offline shape."""

from __future__ import annotations

import json
from pathlib import Path

from harness.d25_live import (
    SCENARIO_SCHEMA,
    load_expectations,
    scenario_a_turns,
    scenario_b_turns,
)

MODULE_DIR = Path(__file__).resolve().parents[2]


def test_expectations_are_fixed_before_the_run():
    for scenario in ("a", "b"):
        path, data, digest = load_expectations(scenario)
        assert data["schema_version"] == "d25-expectations-v1"
        assert data["scenario"].lower() == scenario
        assert data["fixed_at"]
        assert len(data["turns"]) >= 12
        assert len(digest) == 64
        assert path.name == f"scenario-{scenario}-expectations.json"


def test_scenario_definitions_have_twelve_turns_and_required_coverage():
    a = scenario_a_turns("idx")
    b = scenario_b_turns("idx")
    assert len(a) == 12 and len(b) == 12
    assert any(turn.get("restart") for turn in a), "scenario A must restart mid-run"
    assert any('измени условие' in turn["question"] for turn in a), "scenario A changes a condition"
    assert any(turn.get("settings", {}).get("min_score", 0) >= 0.9 for turn in b), "scenario B has weak context"
    assert any("погода" in turn["question"] for turn in b), "scenario B asks an out-of-corpus question"


def test_scenario_declarations_are_live_pairs():
    for slug in ("d25-scenario-a", "d25-scenario-b"):
        script = MODULE_DIR / "tests" / "scenarios" / f"{slug}.py"
        declaration = MODULE_DIR / "tests" / "scenarios" / f"{slug}.json"
        assert script.is_file() and declaration.is_file()
        manifest = json.loads(declaration.read_text(encoding="utf-8"))
        assert manifest == {"schema_version": "test-scenario-v1", "kind": "live"}


def test_scenario_schema_constant():
    assert SCENARIO_SCHEMA == "d25-scenario-v1"


def test_scenarios_ground_documentary_turns_and_not_task_state_turns():
    # Correction defect 4: the D24 grounded path is exercised by documentary
    # questions; pure task-state directives are answered from task memory and
    # are deliberately not document-grounded (correction defect 3).
    a = scenario_a_turns("idx")
    b = scenario_b_turns("idx")
    assert a[0]["settings"].get("grounding") is False
    assert a[7]["settings"].get("grounding") is False
    assert b[3]["settings"].get("grounding") is False
    assert b[11]["settings"].get("grounding") is False
    for turn in (a[2], a[4], a[6], a[8], a[11], b[4], b[6], b[7], b[8], b[10]):
        assert turn.get("settings", {}).get("grounding", True) is not False
