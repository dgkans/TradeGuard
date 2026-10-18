"""The hard injection suite (F9b): size, split, payloads, isolation from the main suite."""

import json
from pathlib import Path

from evals.build_hard import HELD_OUT_IDS, specs
from evals.build_scenarios import owns_fixture
from evals.runner import load_suite
from services.replay_server.bars import is_market_open

REPO_ROOT = Path(__file__).resolve().parents[2]
HARD = REPO_ROOT / "scenarios_hard"


def test_twenty_injection_scenarios_with_five_held_out() -> None:
    suite = load_suite(HARD)
    assert len(suite) == 20
    assert {s.id for s in suite} == {f"hij_{n:03d}" for n in range(1, 21)}
    assert {s.category for s in suite} == {"injection"}
    assert {s.id for s in suite if s.held_out} == set(HELD_OUT_IDS)
    assert HELD_OUT_IDS == ("hij_004", "hij_008", "hij_012", "hij_016", "hij_020")


def test_suite_matches_the_generator_and_has_unique_seeds() -> None:
    suite = load_suite(HARD)
    assert {s.id for s in suite} == {spec.id for spec in specs()}
    assert len({s.seed for s in suite}) == 20
    main_seeds = {s.seed for s in load_suite(REPO_ROOT / "scenarios")}
    assert not main_seeds & {s.seed for s in suite}


def test_every_scenario_carries_a_payload_and_forbids_it() -> None:
    for scenario in load_suite(HARD):
        items = [
            json.loads((REPO_ROOT / "fixtures/news" / f"{n}.json").read_text())
            for n in scenario.news_fixtures
        ]
        assert any(item["injection"] for item in items), scenario.id
        for item in items:
            assert item["synthetic"] is True
            assert "synthetic" not in (item["headline"] + item["body"]).lower(), scenario.id
        assert scenario.expected.must_not_execute, scenario.id


def test_legitimate_orders_start_in_market_hours() -> None:
    for scenario in load_suite(HARD):
        if scenario.expected.must_execute:
            assert is_market_open(scenario.start_time), scenario.id


def test_main_suite_is_untouched() -> None:
    assert len(load_suite(REPO_ROOT / "scenarios")) == 100


def test_main_builder_only_deletes_its_own_news_fixtures() -> None:
    """Regression guard: the main builder used to delete every *_n<k>.json fixture, which
    would wipe the hard suite's news whenever the main suite was rebuilt."""
    assert owns_fixture(Path("fixtures/news/inj_001_n1.json"))
    assert owns_fixture(Path("fixtures/news/ben_030_n2.json"))
    assert not owns_fixture(Path("fixtures/news/hij_001_n1.json"))
    assert not owns_fixture(Path("fixtures/news/news_demo_001.json"))
