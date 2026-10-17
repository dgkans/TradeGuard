"""Clock, no future data, sim broker and determinism."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.replay_server.scenario import load_scenario
from services.replay_server.session import ReplaySession, ToolInputError
from tests.replay.conftest import make_scenario

REPO_ROOT = Path(__file__).resolve().parents[2]


def session_for(data_root: Path, **overrides: object) -> ReplaySession:
    return ReplaySession(
        make_scenario(**overrides),
        bars_root=data_root / "bars",
        news_root=data_root / "news",
    )


def parse(stamp: object) -> datetime:
    assert isinstance(stamp, str)
    return datetime.fromisoformat(stamp)


# --- no future data -------------------------------------------------------------


def test_intraday_bars_stop_at_last_closed_bar(data_root: Path) -> None:
    session = session_for(data_root)  # now = 15:02 UTC
    bars = session.get_bars("AAA", interval="5m", limit=500)["bars"]
    last = bars[-1]
    assert last["start"] == "2026-09-15T14:55:00+00:00"
    assert parse(last["end"]) <= session.now


def test_requested_end_after_now_is_clipped(data_root: Path) -> None:
    session = session_for(data_root)
    far_future = (session.now + timedelta(days=30)).isoformat()
    bars = session.get_bars("AAA", interval="5m", limit=500, end=far_future)["bars"]
    for bar in bars:
        assert parse(bar["end"]) <= session.now


def test_todays_daily_bar_hidden_until_close(data_root: Path) -> None:
    session = session_for(data_root)
    daily = session.get_bars("AAA", interval="1d")["bars"]
    assert [bar["start"][:10] for bar in daily] == ["2026-09-14"]

    session.advance(minutes=60 * 5)  # past 20:00 UTC close
    daily = session.get_bars("AAA", interval="1d")["bars"]
    assert [bar["start"][:10] for bar in daily] == ["2026-09-14", "2026-09-15"]


def test_quote_uses_latest_known_trade(data_root: Path) -> None:
    session = session_for(data_root)  # 15:02
    quote = session.get_quote("AAA")
    # The 15:00 bar (index 18) has opened, so its open is known: 200 + 18.
    # Its high, low and close are not known yet.
    assert quote["price"] == 218.0
    assert parse(quote["as_of"]) <= session.now


def test_quote_before_open_uses_previous_close(data_root: Path) -> None:
    session = session_for(data_root, start_time="2026-09-15T12:00:00Z")
    quote = session.get_quote("AAA")
    assert quote["price"] == 100 + 77 + 0.25  # last day 1 bar close
    assert quote["as_of"] == "2026-09-14T20:00:00+00:00"


def test_news_hidden_until_published(data_root: Path) -> None:
    session = session_for(data_root)
    ids = [item["id"] for item in session.get_news("AAA")["items"]]
    assert "n_late" not in ids
    assert "n_early" in ids

    session.advance(minutes=10)
    ids = [item["id"] for item in session.get_news("AAA")["items"]]
    assert "n_late" in ids


def test_news_view_hides_eval_and_synthetic_labels(data_root: Path) -> None:
    """D013: the agent sees news as an ordinary feed would show it. Trust and synthetic
    labels stay in the fixture files; the gateway adds its own untrusted wrapping."""
    session = session_for(data_root)
    for item in session.get_news()["items"]:
        assert set(item) == {"id", "symbols", "published_at", "source", "headline", "body"}


def test_news_limited_to_scenario_fixtures(data_root: Path) -> None:
    session = session_for(data_root, news_fixtures=["n_early"])
    ids = [item["id"] for item in session.get_news()["items"]]
    assert ids == ["n_early"]


def test_market_order_fills_at_next_bar_open_not_before(data_root: Path) -> None:
    session = session_for(data_root)  # 15:02
    order = session.place_order("AAA", "buy", qty=2)
    assert order["status"] == "new"

    session.advance(minutes=2)  # 15:04, next bar (15:05) has not started
    assert session.list_orders()["orders"][0]["status"] == "new"

    session.advance(minutes=1)  # 15:05
    filled = session.list_orders()["orders"][0]
    assert filled["status"] == "filled"
    assert filled["filled_avg_price"] == 219.0  # open of 15:05 bar, index 19
    assert parse(filled["filled_at"]) <= session.now


def test_limit_fill_inside_bar_waits_for_bar_close(data_root: Path) -> None:
    session = session_for(data_root)  # 15:02, 15:05 bar opens 219 with low 218.5
    order = session.place_order("AAA", "buy", qty=1, order_type="limit", limit_price=218.6)
    session.advance(minutes=5)  # 15:07, bar still open: its low is not known yet
    assert session.list_orders()["orders"][0]["status"] == "new"
    session.advance(minutes=3)  # 15:10, bar closed
    filled = session.list_orders()["orders"][0]
    assert filled["id"] == order["id"]
    assert filled["status"] == "filled"
    assert filled["filled_avg_price"] == 218.6


def test_future_sweep_on_demo_scenario() -> None:
    """Walk the real demo data in 25 minute steps and check every timestamp tools return."""
    scenario = load_scenario("demo", REPO_ROOT / "scenarios")
    session = ReplaySession(
        scenario, bars_root=REPO_ROOT / "fixtures/bars", news_root=REPO_ROOT / "fixtures/news"
    )
    for _ in range(40):
        now = session.now
        for symbol in scenario.watchlist:
            for interval in ("5m", "1d"):
                for bar in session.get_bars(symbol, interval=interval, limit=500)["bars"]:
                    assert parse(bar["end"]) <= now
            assert parse(session.get_quote(symbol)["as_of"]) <= now
        for item in session.get_news(limit=100)["items"]:
            assert parse(item["published_at"]) <= now
        session.advance(minutes=25)


# --- sim broker -------------------------------------------------------------------


def test_rejects_buy_over_buying_power(data_root: Path) -> None:
    session = session_for(data_root, starting_cash=1000)
    order = session.place_order("AAA", "buy", qty=20)  # 20 * 218 = 4360
    assert order["status"] == "rejected"
    assert "buying power" in order["reject_reason"]


def test_rejects_notional_over_buying_power(data_root: Path) -> None:
    session = session_for(data_root, starting_cash=1000)
    order = session.place_order("AAA", "buy", notional=1500)
    assert order["status"] == "rejected"
    assert "buying power" in order["reject_reason"]


def test_open_orders_reserve_buying_power(data_root: Path) -> None:
    session = session_for(data_root, starting_cash=1000)
    first = session.place_order("AAA", "buy", notional=600)
    second = session.place_order("AAA", "buy", notional=600)
    assert first["status"] == "new"
    assert second["status"] == "rejected"
    assert session.get_account()["buying_power"] == 400.0


def test_rejects_sell_over_position(data_root: Path) -> None:
    session = session_for(data_root, positions={"AAA": 3})
    order = session.place_order("AAA", "sell", qty=4)
    assert order["status"] == "rejected"
    assert "position" in order["reject_reason"]


def test_fill_updates_cash_and_position(data_root: Path) -> None:
    session = session_for(data_root, starting_cash=10000)
    session.place_order("AAA", "buy", qty=10)
    session.advance(minutes=3)
    account = session.get_account()
    assert account["cash"] == 10000 - 10 * 219.0
    positions = session.get_positions()["positions"]
    assert positions == [
        {
            "symbol": "AAA",
            "qty": 10.0,
            "avg_entry_price": 219.0,
            "market_price": 219.0,
            "market_value": 2190.0,
            "unrealized_pl": 0.0,
        }
    ]


def test_cancel_prevents_fill(data_root: Path) -> None:
    session = session_for(data_root)
    order = session.place_order("AAA", "buy", qty=1)
    canceled = session.cancel_order(order["id"])
    assert canceled["status"] == "canceled"
    session.advance(minutes=30)
    assert session.list_orders()["orders"][0]["status"] == "canceled"
    assert session.get_positions()["positions"] == []


def test_unfilled_limit_expires_at_close(data_root: Path) -> None:
    session = session_for(data_root)
    session.place_order("AAA", "buy", qty=1, order_type="limit", limit_price=1.0)
    session.advance(minutes=60 * 6)
    assert session.list_orders()["orders"][0]["status"] == "expired"
    assert session.get_account()["buying_power"] == 10000.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"qty": 1, "notional": 100},
        {},
        {"qty": -1},
        {"qty": 1, "order_type": "limit"},
        {"notional": 100, "order_type": "limit", "limit_price": 100},
        {"qty": 1, "order_type": "stop"},
    ],
)
def test_invalid_order_arguments_raise(data_root: Path, kwargs: dict[str, Any]) -> None:
    session = session_for(data_root)
    with pytest.raises(ToolInputError):
        session.place_order("AAA", "buy", **kwargs)


def test_unknown_symbol_raises(data_root: Path) -> None:
    session = session_for(data_root)
    with pytest.raises(ToolInputError, match="ZZZZ"):
        session.get_quote("ZZZZ")


def test_advance_must_move_forward(data_root: Path) -> None:
    session = session_for(data_root)
    with pytest.raises(ToolInputError):
        session.advance(minutes=0)


# --- determinism ------------------------------------------------------------------


def demo_transcript(seed: int | None = None) -> list[Any]:
    scenario = load_scenario("demo", REPO_ROOT / "scenarios")
    if seed is not None:
        scenario = scenario.model_copy(update={"seed": seed})
    session = ReplaySession(
        scenario, bars_root=REPO_ROOT / "fixtures/bars", news_root=REPO_ROOT / "fixtures/news"
    )
    out: list[Any] = []
    out.append(session.get_bars("NVDA", interval="5m", limit=20))
    out.append(session.get_quote("AMD"))
    out.append(session.get_news(limit=20))
    out.append(session.place_order("NVDA", "buy", notional=500))
    price = session.get_quote("AAPL")["price"]
    out.append(session.place_order("AAPL", "buy", qty=2, order_type="limit", limit_price=price))
    session.advance(minutes=45)
    out.append(session.list_orders())
    out.append(session.get_positions())
    out.append(session.get_account())
    return out


def test_same_seed_is_identical() -> None:
    assert demo_transcript() == demo_transcript()


def test_seed_controls_order_ids() -> None:
    first = demo_transcript(seed=1)
    second = demo_transcript(seed=2)
    assert first[3]["id"] != second[3]["id"]
    # Market data and fills don't depend on the seed, only ids do.
    assert first[0] == second[0]
    assert first[3]["status"] == second[3]["status"]


def test_session_starts_at_scenario_time(data_root: Path) -> None:
    session = session_for(data_root)
    assert session.now == datetime(2026, 9, 15, 15, 2, tzinfo=UTC)
    clock = session.get_clock()
    assert clock["market_open"] is True


def test_order_at_bar_boundary_fills_at_next_bar(data_root: Path) -> None:
    """Regression: an order placed exactly at 15:00:00 must not fill at the 15:00 open."""
    session = session_for(data_root, start_time="2026-09-15T15:00:00Z")
    order = session.place_order("AAA", "buy", qty=1)
    assert order["status"] == "new"
    session.advance(minutes=5)
    filled = session.list_orders()["orders"][0]
    assert filled["filled_avg_price"] == 219.0  # 15:05 open, not 15:00 open (218)


def test_prices_have_no_float_noise() -> None:
    scenario = load_scenario("demo", REPO_ROOT / "scenarios")
    session = ReplaySession(
        scenario, bars_root=REPO_ROOT / "fixtures/bars", news_root=REPO_ROOT / "fixtures/news"
    )
    price = session.get_quote("NVDA")["price"]
    assert price == round(price, 4)
