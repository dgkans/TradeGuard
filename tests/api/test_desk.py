"""The shared desk clock (F10e): one replayed market time for every trader and the landing."""

from datetime import UTC, datetime
from pathlib import Path

from services.api.desk import DeskClock, trading_sessions

REPO_ROOT = Path(__file__).resolve().parents[2]
BARS = REPO_ROOT / "fixtures/bars"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_sessions_come_from_the_bar_data() -> None:
    sessions = trading_sessions(BARS)
    assert sessions[at("2026-10-01T13:30:00")] == at("2026-10-01T20:00:00")
    assert max(sessions) == at("2026-10-16T13:30:00")  # last day in the fixtures
    assert at("2026-10-03T13:30:00") not in sessions  # a Saturday


def test_steps_five_minutes_through_the_day() -> None:
    clock = DeskClock(BARS, start=at("2026-10-01T14:30:00"))
    assert clock.tick() == at("2026-10-01T14:35:00")
    assert clock.tick() == at("2026-10-01T14:40:00")


def test_reaches_the_close_then_jumps_to_the_next_open() -> None:
    clock = DeskClock(BARS, start=at("2026-10-01T19:55:00"))
    assert clock.tick() == at("2026-10-01T20:00:00")  # the close
    assert clock.tick() == at("2026-10-02T13:30:00")  # next morning's open


def test_jumps_over_the_weekend() -> None:
    clock = DeskClock(BARS, start=at("2026-10-02T20:00:00"))  # Friday's close
    assert clock.tick() == at("2026-10-05T13:30:00")  # Monday


def test_stops_at_the_end_of_the_data() -> None:
    clock = DeskClock(BARS, start=at("2026-10-16T20:00:00"))
    assert clock.tick() is None
    assert clock.ended
    assert clock.now == at("2026-10-16T20:00:00")


def test_a_start_outside_market_hours_moves_to_the_next_open() -> None:
    clock = DeskClock(BARS, start=at("2026-10-03T15:00:00"))  # Saturday
    assert clock.tick() == at("2026-10-05T13:30:00")


def test_minutes_between_for_catching_markets_up() -> None:
    clock = DeskClock(BARS, start=at("2026-10-01T14:30:00"))
    clock.tick()
    clock.tick()
    assert clock.minutes_since(at("2026-10-01T14:30:00")) == 10
    assert clock.minutes_since(clock.now) == 0
