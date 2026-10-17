"""detect_patterns and scan_watchlist on the replay session."""

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from services.replay_server.scenario import load_scenario
from services.replay_server.session import ReplaySession, ToolInputError
from tests.replay.conftest import make_scenario

REPO_ROOT = Path(__file__).resolve().parents[2]


def timestamps(value: Any) -> list[datetime]:
    """Every ISO timestamp anywhere in a tool result."""
    found: list[datetime] = []
    if isinstance(value, dict):
        for item in value.values():
            found.extend(timestamps(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(timestamps(item))
    elif isinstance(value, str) and len(value) >= 19 and value[4] == "-" and value[10] == "T":
        found.append(datetime.fromisoformat(value))
    return found


def demo_session() -> ReplaySession:
    return ReplaySession(
        load_scenario("demo", REPO_ROOT / "scenarios"),
        bars_root=REPO_ROOT / "fixtures/bars",
        news_root=REPO_ROOT / "fixtures/news",
    )


def test_detect_patterns_reports_prior_day_high_break(data_root: Path) -> None:
    session = ReplaySession(
        make_scenario(), bars_root=data_root / "bars", news_root=data_root / "news"
    )  # 15:02 on day 2; day 1 high was 178.5, day 2 opens near 200
    result = session.detect_patterns("AAA")
    fired = {}
    for detection in result["patterns"]:
        fired[detection["pattern"]] = detection
    assert "prior_day_high_break" in fired
    breakout = fired["prior_day_high_break"]
    assert breakout["details"]["prior_day_high"] == 178.5
    assert breakout["bars"][1]["start"] == "2026-09-15T13:30:00+00:00"
    assert breakout["rule"].startswith("prior_day_high_break:")
    assert "inside_bar" in result["not_fired"]
    for stamp in timestamps(result):
        assert stamp <= session.now


def test_detect_patterns_before_open_has_no_intraday_check(data_root: Path) -> None:
    session = ReplaySession(
        make_scenario(start_time="2026-09-15T12:00:00Z"),
        bars_root=data_root / "bars",
        news_root=data_root / "news",
    )
    result = session.detect_patterns("AAA")
    reasons = {}
    for item in result["not_evaluated"]:
        reasons[item["pattern"]] = item["reason"]
    assert "prior_day_high_break" in reasons


def test_scan_watchlist_never_leaks_future_on_demo() -> None:
    session = demo_session()
    for _ in range(12):
        result = session.scan_watchlist(["NVDA", "AMD", "AAPL"])
        assert len(result["results"]) == 3
        for stamp in timestamps(result):
            assert stamp <= session.now
        session.advance(minutes=35)


def test_scan_watchlist_reports_bad_symbol_without_failing() -> None:
    result = demo_session().scan_watchlist(["NVDA", "ZZZZ"])
    by_symbol = {}
    for row in result["results"]:
        by_symbol[row["symbol"]] = row
    assert "error" in by_symbol["ZZZZ"]
    assert "patterns" in by_symbol["NVDA"]


def test_scan_watchlist_limits_size() -> None:
    with pytest.raises(ToolInputError):
        demo_session().scan_watchlist(["NVDA"] * 26)
    with pytest.raises(ToolInputError):
        demo_session().scan_watchlist([])


def test_scan_is_deterministic() -> None:
    assert demo_session().scan_watchlist(["NVDA", "AMD"]) == demo_session().scan_watchlist(
        ["NVDA", "AMD"]
    )
