"""Live prices (F10f): the shared feed and a trader's live paper market, against a fake Yahoo."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.live import LiveFeed, LiveMarket, parse_bars
from services.replay_server.scenario import load_scenario
from services.replay_server.session import ToolInputError
from tests.api.fake_yahoo import OPEN_MOMENT, Clock, FakeYahoo

ROOT = Path(__file__).resolve().parents[2]
SYMBOLS = ["SPY", "NVDA", "AMD", "AAPL"]


Setup = tuple[LiveFeed, LiveMarket, FakeYahoo, Clock]


async def make(at: datetime = OPEN_MOMENT, cash: float = 10000) -> Setup:
    clock = Clock(at)
    yahoo = FakeYahoo(ROOT, clock)
    feed = LiveFeed(SYMBOLS, yahoo, clock=clock)
    await feed.refresh()
    scenario = load_scenario("desk", ROOT / "scenarios")
    return feed, LiveMarket(scenario, feed, cash=cash), yahoo, clock


def test_parse_bars_keeps_the_regular_session_and_skips_empty_rows() -> None:
    open_ts = int(datetime(2026, 10, 16, 13, 30, tzinfo=UTC).timestamp())
    close_print = int(datetime(2026, 10, 16, 20, 0, tzinfo=UTC).timestamp())
    result = {"timestamp": [open_ts, open_ts + 300, close_print],
              "indicators": {"quote": [{"open": [1.0, None, 3.0], "high": [1.5, None, 3.0],
                                        "low": [0.5, None, 3.0], "close": [1.2, None, 3.0],
                                        "volume": [10, None, 0]}]}}  # fmt: skip
    bars = parse_bars("NVDA", "5m", result)
    assert [(bar.open, bar.close, bar.volume) for bar in bars] == [(1.0, 1.2, 10)]


async def test_feed_has_prices_bars_session_and_headlines() -> None:
    feed, _market, _yahoo, clock = await make()
    quote = feed.quote("NVDA")
    assert quote is not None and quote.price > 0
    assert feed.market_open()
    assert feed.all_bars("NVDA", "5m") and feed.all_bars("NVDA", "1d")
    assert feed.headlines("NVDA")[0].headline.startswith("NVDA")
    status = feed.status()
    assert status["live"] and not status["stale"] and status["source"] == "Yahoo Finance"
    clock.move(hours=6)  # after the close
    assert not feed.market_open()


async def test_previous_close_is_the_session_before_the_quote() -> None:
    feed, _market, _yahoo, _clock = await make()
    daily = feed.all_bars("NVDA", "1d")
    before = [bar for bar in daily if bar.start.date() < OPEN_MOMENT.date()]
    assert feed.previous_close("NVDA") == before[-1].close


async def test_a_market_order_fills_now_at_the_live_price() -> None:
    _feed, market, yahoo, _clock = await make()
    yahoo.prices["AMD"] = 150.0
    await _feed.refresh()
    order = market.place_order("AMD", "buy", notional=600)
    assert order["status"] == "filled"
    assert order["filled_avg_price"] == 150.0
    assert market.get_positions()["positions"][0]["qty"] == 4.0
    assert market.get_account()["cash"] == 9400.0
    assert market.get_account()["mode"] == "paper (live prices)"


async def test_a_limit_order_waits_for_the_price_then_fills() -> None:
    feed, market, yahoo, clock = await make()
    yahoo.prices["NVDA"] = 200.0
    await feed.refresh()
    order = market.place_order("NVDA", "buy", qty=2, order_type="limit", limit_price=190)
    assert order["status"] == "new"
    assert not market.settle()
    yahoo.prices["NVDA"] = 189.5
    clock.move(minutes=1)
    await feed.refresh()
    assert market.settle()
    filled = market.list_orders("closed")["orders"][0]
    assert filled["status"] == "filled" and filled["filled_avg_price"] == 189.5


async def test_orders_wait_while_closed_and_expire_at_the_close() -> None:
    feed, market, _yahoo, clock = await make(at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    assert not feed.market_open()  # 08:00 New York
    order = market.place_order("AAPL", "buy", notional=300)
    assert order["status"] == "new"
    clock.at = datetime(2026, 10, 1, 20, 1, tzinfo=UTC)  # past 16:00 without filling
    feed.session = None  # Yahoo hasn't answered since: unknown session counts as closed
    market.settle()
    assert market.list_orders("all")["orders"][0]["status"] == "expired"


async def test_stale_prices_refuse_new_orders() -> None:
    feed, market, yahoo, clock = await make()
    yahoo.failing = True
    clock.move(minutes=10)
    await feed.refresh()
    assert feed.last_error is not None
    assert feed.status()["stale"]
    with pytest.raises(ToolInputError, match="unavailable"):
        market.place_order("AMD", "buy", notional=100)


async def test_a_rate_limit_backs_off() -> None:
    feed, _market, yahoo, clock = await make()
    yahoo.rate_limited = True
    clock.move(minutes=1)
    await feed.refresh()
    calls = len(yahoo.calls)
    clock.move(seconds=30)
    await feed.refresh()
    assert len(yahoo.calls) == calls  # still backing off: no new requests


async def test_the_book_survives_a_snapshot_round_trip() -> None:
    feed, market, _yahoo, _clock = await make()
    market.place_order("AMD", "buy", notional=500)
    market.place_order("NVDA", "buy", qty=1, order_type="limit", limit_price=1)
    book = market.snapshot()
    again = LiveMarket(market.scenario, feed, cash=10000)
    again.load(book)
    assert again.get_account() == market.get_account()
    assert again.get_positions() == market.get_positions()
    assert again.list_orders("all") == market.list_orders("all")


async def test_tools_read_live_data() -> None:
    _feed, market, _yahoo, _clock = await make()
    assert market.get_clock()["market_open"]
    assert market.get_news("NVDA")["items"][0]["source"] == "Example Wire"
    assert market.get_bars("NVDA", "5m", 5)["bars"]
    assert market.detect_patterns("NVDA")["symbol"] == "NVDA"
    with pytest.raises(ToolInputError):
        market.get_quote("ZZZZ")
    with pytest.raises(ToolInputError):
        market.advance(5)
