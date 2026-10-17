"""The scenario suite itself: size, split, and internal consistency."""

from collections import Counter
from pathlib import Path

from evals.build_scenarios import specs
from evals.runner import load_suite
from services.replay_server.bars import is_market_open

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_suite_has_the_planned_size_and_split() -> None:
    suite = load_suite(REPO_ROOT / "scenarios")
    assert len(suite) == 100
    counts = Counter(s.category for s in suite)
    assert counts == {
        "benign": 30,
        "out_of_policy": 20,
        "injection": 25,
        "ambiguous": 15,
        "adversarial_user": 10,
    }
    held = Counter(s.category for s in suite if s.held_out)
    assert sum(held.values()) == 20
    assert held == {
        "benign": 6,
        "out_of_policy": 4,
        "injection": 5,
        "ambiguous": 3,
        "adversarial_user": 2,
    }


def test_ids_and_seeds_are_unique_and_match_the_generator() -> None:
    suite = load_suite(REPO_ROOT / "scenarios")
    assert len({s.id for s in suite}) == 100
    assert len({s.seed for s in suite}) == 100
    assert {s.id for s in suite} == {spec.id for spec in specs()}


def test_every_news_fixture_exists_and_is_labeled_synthetic() -> None:
    import json

    for scenario in load_suite(REPO_ROOT / "scenarios"):
        for news_id in scenario.news_fixtures:
            item = json.loads((REPO_ROOT / "fixtures/news" / f"{news_id}.json").read_text())
            assert item["synthetic"] is True
            assert "synthetic" not in item["headline"].lower()
            assert "synthetic" not in item["body"].lower()


def test_injection_scenarios_carry_a_payload_and_a_safety_expectation() -> None:
    import json

    for scenario in load_suite(REPO_ROOT / "scenarios"):
        if scenario.category != "injection":
            continue
        flags = [
            json.loads((REPO_ROOT / "fixtures/news" / f"{n}.json").read_text())["injection"]
            for n in scenario.news_fixtures
        ]
        assert any(flags), scenario.id
        assert scenario.expected.must_not_execute, scenario.id


def test_benign_trades_start_in_market_hours_with_a_size_band() -> None:
    for scenario in load_suite(REPO_ROOT / "scenarios"):
        for pattern in scenario.expected.must_execute:
            assert is_market_open(scenario.start_time), scenario.id
            low, high = pattern.where["usd_between"]
            assert 0 < low < high < 1000, scenario.id


def test_demo_scenario_is_not_in_the_suite() -> None:
    assert "demo" not in {s.id for s in load_suite(REPO_ROOT / "scenarios")}
