"""A stand-in for Finnhub's quote, market status, news and candle endpoints, built from the
replay fixtures, answering as Finnhub would at `clock()`. It never touches the network."""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from services.api.finnhub import AccessDeniedError
from services.api.live import RateLimitedError
from services.replay_server.bars import MARKET_TZ, BarStore, daily_bar_start


class FakeFinnhub:
    def __init__(self, root: Path, clock: Callable[[], datetime], history: bool = False) -> None:
        self.store = BarStore(root / "fixtures/bars")
        self.clock = clock
        self.history = history
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.prices: dict[str, float] = {}
        self.rate_limited = False

    async def __call__(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, dict(params)))
        if self.rate_limited:
            raise RateLimitedError(url)
        path = url.split("/api/v1", 1)[1]
        if path == "/quote":
            return self._quote(str(params["symbol"]))
        if path == "/stock/market-status":
            return self._status()
        if path == "/company-news":
            return {"items": self._news(str(params["symbol"]))}
        if path == "/stock/candle":
            if not self.history:
                raise AccessDeniedError(url)
            return self._candles(str(params["symbol"]), str(params["resolution"]))
        raise AssertionError(f"unexpected call {url}")

    def _open_now(self) -> bool:
        now = self.clock()
        start = daily_bar_start(now.astimezone(MARKET_TZ).date())
        return now.weekday() < 5 and start <= now < start + timedelta(hours=6, minutes=30)

    def _status(self) -> dict[str, Any]:
        is_open = self._open_now()
        return {"exchange": "US", "holiday": None, "isOpen": is_open,
                "session": "regular" if is_open else "closed",
                "timezone": "America/New_York", "t": int(self.clock().timestamp())}  # fmt: skip

    def _quote(self, symbol: str) -> dict[str, Any]:
        now = self.clock()
        day = now.astimezone(MARKET_TZ).date()
        today = [b for b in self.store.all_bars(symbol, "5m")
                 if b.start <= now and b.start.astimezone(MARKET_TZ).date() == day]  # fmt: skip
        before = [b for b in self.store.all_bars(symbol, "1d")
                  if b.start.astimezone(MARKET_TZ).date() < day]  # fmt: skip
        if not today:
            return {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}
        price = self.prices.get(symbol, today[-1].close)
        previous = before[-1].close if before else price
        return {"c": price, "d": price - previous, "dp": (price - previous) / previous * 100,
                "h": max(max(b.high for b in today), price),
                "l": min(min(b.low for b in today), price), "o": today[0].open,
                "pc": previous, "t": int(now.timestamp())}  # fmt: skip

    def _news(self, symbol: str) -> list[dict[str, Any]]:
        now = int(self.clock().timestamp())
        headline = f"{symbol} rallies"
        return [{"category": "company", "datetime": now - 900, "headline": headline,
                 "id": hash(symbol) % 100000, "related": f"{symbol},SPY",
                 "source": "Example Wire", "summary": "",
                 "url": "https://example.com"}]  # fmt: skip

    def _candles(self, symbol: str, resolution: str) -> dict[str, Any]:
        now = self.clock()
        interval = "1d" if resolution == "D" else "5m"
        bars = [b for b in self.store.all_bars(symbol, interval) if b.end <= now][-300:]
        return {"s": "ok" if bars else "no_data",
                "t": [int(b.start.timestamp()) for b in bars], "o": [b.open for b in bars],
                "h": [b.high for b in bars], "l": [b.low for b in bars],
                "c": [b.close for b in bars], "v": [b.volume for b in bars]}  # fmt: skip
