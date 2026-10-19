"""Live prices from Finnhub (F12), against a fake Finnhub built from the fixtures."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.api.finnhub import AccessDeniedError, FinnhubFeed, finnhub_fetch
from services.api.live import LiveFeed, LiveMarket, RateLimitedError
from services.replay_server.scenario import load_scenario
from tests.api.fake_finnhub import FakeFinnhub
from tests.api.fake_yahoo import OPEN_MOMENT, Clock

ROOT = Path(__file__).resolve().parents[2]
SYMBOLS = ["SPY", "QQQ", "NVDA", "AMD"]
SUNDAY = datetime(2026, 10, 4, 16, 0, tzinfo=UTC)


async def make(at: datetime = OPEN_MOMENT, history: bool = False,
               ) -> tuple[FinnhubFeed, FakeFinnhub, Clock]:  # fmt: skip
    clock = Clock(at)
    finnhub = FakeFinnhub(ROOT, clock, history=history)
    feed = FinnhubFeed(SYMBOLS, finnhub, history_root=ROOT / "fixtures/bars", clock=clock,
                       slow_calls_per_poll=len(SYMBOLS) * 2)  # fmt: skip
    await feed.refresh()
    return feed, finnhub, clock


async def test_quotes_previous_close_and_open_status() -> None:
    feed, finnhub, _clock = await make()
    quote = feed.quote("NVDA")
    assert quote is not None and quote.price > 0
    previous = feed.previous_close("NVDA")
    assert previous == finnhub._quote("NVDA")["pc"]
    assert feed.market_open()
    status = feed.status()
    assert status["source"] == "Finnhub" and not status["stale"] and status["problem"] is None
    assert feed.headlines("NVDA")[0].headline == "NVDA rallies"


async def test_without_history_bars_come_from_quotes_and_saved_history() -> None:
    feed, finnhub, clock = await make()
    assert feed.history is False  # the probe was refused: a free key
    for step, price in ((2, 101.0), (2, 103.0), (2, 99.0), (5, 100.0)):
        clock.move(minutes=step)
        finnhub.prices["NVDA"] = price
        await feed.refresh()
    intraday = feed.all_bars("NVDA", "5m")
    assert intraday and all(bar.volume == 0 for bar in intraday)
    daily = feed.all_bars("NVDA", "1d")
    today = clock().date()
    assert daily[-1].start.date() == today  # today's live bar
    assert daily[-1].close == 100.0
    assert all(bar.start.date() < today for bar in daily[:-1])  # saved history before it
    market = LiveMarket(load_scenario("desk", ROOT / "scenarios"), feed, cash=1000)
    assert market.detect_patterns("NVDA")["symbol"] == "NVDA"


async def test_with_history_bars_come_from_finnhub() -> None:
    feed, _finnhub, _clock = await make(history=True)
    assert feed.history is True
    daily = feed.all_bars("NVDA", "1d")
    assert len(daily) > 50 and daily[-1].volume > 0


async def test_a_closed_market_says_when_it_opens_and_orders_wait() -> None:
    feed, _finnhub, _clock = await make(at=SUNDAY)
    status = feed.status()
    assert status["market_open"] is False
    assert status["session_start"] == "2026-10-05T13:30:00+00:00"  # Monday 09:30 New York
    market = LiveMarket(load_scenario("desk", ROOT / "scenarios"), feed, cash=10000)
    assert market.get_clock()["market_open"] is False


async def test_a_market_order_fills_at_the_finnhub_price() -> None:
    feed, finnhub, _clock = await make()
    finnhub.prices["AMD"] = 125.0
    await feed.refresh()
    market = LiveMarket(load_scenario("desk", ROOT / "scenarios"), feed, cash=10000)
    order = market.place_order("AMD", "buy", notional=500)
    assert order["status"] == "filled" and order["filled_avg_price"] == 125.0


async def test_a_rate_limit_backs_off_and_is_reported() -> None:
    feed, finnhub, clock = await make()
    finnhub.rate_limited = True
    clock.move(seconds=30)
    await feed.refresh()
    assert feed.status()["problem"] == "RateLimitedError"
    calls = len(finnhub.calls)
    clock.move(seconds=30)
    await feed.refresh()
    assert len(finnhub.calls) == calls


async def test_the_key_travels_in_a_header_never_the_url() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/stock/candle"):
            return httpx.Response(403, json={"error": "You don't have access to this resource."})
        if request.url.path.endswith("/quote") and request.url.params["symbol"] == "LIMIT":
            return httpx.Response(429, json={"error": "API limit reached"})
        return httpx.Response(200, json={"c": 1.0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        fetch = finnhub_fetch(client, "fh_secret_key")
        assert await fetch("https://finnhub.io/api/v1/quote", {"symbol": "NVDA"}) == {"c": 1.0}
        with pytest.raises(AccessDeniedError):
            await fetch("https://finnhub.io/api/v1/stock/candle", {"symbol": "NVDA"})
        with pytest.raises(RateLimitedError):
            await fetch("https://finnhub.io/api/v1/quote", {"symbol": "LIMIT"})
    assert all(r.headers["x-finnhub-token"] == "fh_secret_key" for r in seen)
    assert all("fh_secret_key" not in str(r.url) for r in seen)


def test_the_app_picks_the_feed_from_settings() -> None:
    from services.api.app import live_feed
    from services.common.config import load_settings

    def feed(**env: str) -> Any:
        return live_feed(load_settings(env_file=None, environ=env), {})

    assert feed(LIVE_PRICES="false") is None
    assert feed(LIVE_PRICE_SOURCE="finnhub") is None  # no key yet: replayed prices
    assert isinstance(feed(LIVE_PRICE_SOURCE="finnhub", FINNHUB_API_KEY="k"), FinnhubFeed)
    yahoo = feed()
    assert isinstance(yahoo, LiveFeed) and not isinstance(yahoo, FinnhubFeed)
