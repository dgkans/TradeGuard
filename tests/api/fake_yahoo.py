"""A stand-in for Yahoo Finance's chart and search endpoints, built from the replay fixtures.

It answers like Yahoo does at `clock()`: bars up to that moment (including the bar in
progress), the latest price, and the day's regular session. Tests move the clock or set
prices to drive fills. It never touches the network.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from services.api.live import RateLimitedError
from services.replay_server.bars import MARKET_TZ, BarStore, daily_bar_start


class FakeYahoo:
    def __init__(self, root: Path, clock: Callable[[], datetime]) -> None:
        self.store = BarStore(root / "fixtures/bars")
        self.clock = clock
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.prices: dict[str, float] = {}  # overrides for the latest price
        self.failing = False
        self.rate_limited = False

    async def __call__(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, dict(params)))
        if self.rate_limited:
            raise RateLimitedError(url)
        if self.failing:
            raise ConnectionError("yahoo is down")
        if "/search" in url:
            return self._search(str(params["q"]))
        symbol = url.rstrip("/").split("/")[-1]
        return self._chart(symbol, str(params["interval"]))

    def _chart(self, symbol: str, interval: str) -> dict[str, Any]:
        now = self.clock()
        bars = [bar for bar in self.store.all_bars(symbol, interval) if bar.start <= now]
        if interval == "5m":
            bars = [bar for bar in bars if bar.start >= now - timedelta(days=7)]
        intraday = [b for b in self.store.all_bars(symbol, "5m") if b.start <= now]
        last = intraday[-1] if intraday else None
        price = self.prices.get(symbol, (last.close if last and last.end <= now
                                         else last.open if last else 0.0))  # fmt: skip
        day = now.astimezone(MARKET_TZ).date()
        start = daily_bar_start(day)
        meta = {
            "symbol": symbol,
            "regularMarketPrice": price,
            "regularMarketTime": int(min(now, last.end if last else now).timestamp()),
            "currentTradingPeriod": {"regular": {
                "start": int(start.timestamp()),
                "end": int((start + timedelta(hours=6, minutes=30)).timestamp()),
            }},
        }  # fmt: skip
        return {"chart": {"result": [{
            "meta": meta,
            "timestamp": [int(bar.start.timestamp()) for bar in bars],
            "indicators": {"quote": [{
                "open": [bar.open for bar in bars], "high": [bar.high for bar in bars],
                "low": [bar.low for bar in bars], "close": [bar.close for bar in bars],
                "volume": [bar.volume for bar in bars],
            }]},
        }]}}  # fmt: skip

    def _search(self, symbol: str) -> dict[str, Any]:
        now = self.clock()
        return {"news": [
            {"uuid": f"n-{symbol}-1", "title": f"{symbol} shares move as investors weigh earnings",
             "publisher": "Example Wire", "providerPublishTime": int(now.timestamp()) - 600,
             "relatedTickers": [symbol]},
            {"uuid": "n-market", "title": "Stocks mixed ahead of the Fed decision",
             "publisher": "Example Wire", "providerPublishTime": int(now.timestamp()) - 3600,
             "relatedTickers": ["SPY", symbol]},
        ]}  # fmt: skip


class Clock:
    """A settable clock: `clock()` is now; `clock.at = ...` moves it."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def move(self, **delta: float) -> None:
        self.at = self.at + timedelta(**delta)


# Thursday 1 Oct 2026, 11:00 New York: the market is open.
OPEN_MOMENT = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
