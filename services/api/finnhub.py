"""Live prices from Finnhub's free plan (F12): what the desk uses on Render, where Yahoo
answers every request with 429.

Free key, about 60 calls a minute. Each poll costs one market status call, one quote per
symbol, and two "slow" calls taken in turn (news, and price history when the key has it).
The key goes in the `X-Finnhub-Token` header, never in a URL or a log line.

Price history (candles) may not be on the free plan. The feed probes once:
- with history: daily and 5 minute bars come from Finnhub;
- without: 5 minute bars are built from the quotes it collects while awake, and daily bars
  are the saved fixture history plus today's live bar (open, high, low, last).
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from services.api.live import (
    Fetch,
    Headline,
    LiveFeed,
    Quote,
    RateLimitedError,
    Session,
)
from services.replay_server.bars import (
    INTERVALS,
    MARKET_CLOSE,
    MARKET_OPEN,
    MARKET_TZ,
    Bar,
    BarStore,
    daily_bar_start,
)

logger = logging.getLogger(__name__)
BASE = "https://finnhub.io/api/v1"
SOURCE = "Finnhub"
HISTORY_DAYS = 365
INTRADAY_DAYS = 5


class AccessDeniedError(Exception):
    """The key isn't allowed this endpoint (Finnhub answers 403 for premium data)."""


def finnhub_fetch(client: httpx.AsyncClient, api_key: str) -> Fetch:
    async def fetch(url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await client.get(url, params=params, headers={"X-Finnhub-Token": api_key})
        if response.status_code == 429:
            raise RateLimitedError(url)
        if response.status_code in (401, 403):
            raise AccessDeniedError(url)
        response.raise_for_status()
        data: Any = response.json()
        return data if isinstance(data, dict) else {"items": data}

    return fetch


def next_regular_open(now: datetime) -> datetime:
    """The next 09:30 New York open after `now`, on a weekday (holidays aren't known)."""
    local = now.astimezone(MARKET_TZ)
    day = local.date()
    if local.time() >= MARKET_OPEN:
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, MARKET_OPEN, tzinfo=MARKET_TZ).astimezone(UTC)


def todays_session(now: datetime) -> Session:
    day = now.astimezone(MARKET_TZ).date()
    start = datetime.combine(day, MARKET_OPEN, tzinfo=MARKET_TZ).astimezone(UTC)
    end = datetime.combine(day, MARKET_CLOSE, tzinfo=MARKET_TZ).astimezone(UTC)
    return Session(start, end)


def candle_bars(symbol: str, interval: str, data: dict[str, Any]) -> list[Bar]:
    """Bars from a Finnhub candle answer (`s` is "ok" or "no_data")."""
    if data.get("s") != "ok":
        return []
    length = INTERVALS[interval]
    bars = []
    for i, stamp in enumerate(data.get("t") or []):
        moment = datetime.fromtimestamp(int(stamp), UTC)
        if interval == "1d":
            start = daily_bar_start(moment.astimezone(MARKET_TZ).date())
        else:
            start = moment
            local = start.astimezone(MARKET_TZ).time()
            if not MARKET_OPEN <= local < MARKET_CLOSE:
                continue
        values = [data[key][i] for key in ("o", "h", "l", "c")]
        volume = int((data.get("v") or [0] * (i + 1))[i] or 0)
        o, h, low, c = (round(float(v), 4) for v in values)
        bars.append(Bar(symbol, start, start + length, o, h, low, c, volume))
    return bars


def bars_from_samples(symbol: str, samples: list[tuple[datetime, float]]) -> list[Bar]:
    """5 minute bars built from collected quotes (open, high, low, last of each window)."""
    length = INTERVALS["5m"]
    windows: dict[datetime, list[float]] = {}
    for moment, price in samples:
        local = moment.astimezone(MARKET_TZ)
        if not MARKET_OPEN <= local.time() < MARKET_CLOSE:
            continue
        start = moment.replace(minute=moment.minute - moment.minute % 5, second=0,
                               microsecond=0)  # fmt: skip
        windows.setdefault(start, []).append(price)
    bars = []
    for start in sorted(windows):
        prices = windows[start]
        bars.append(Bar(symbol, start, start + length, prices[0], max(prices), min(prices),
                        prices[-1], 0))  # fmt: skip
    return bars


class FinnhubFeed(LiveFeed):
    source = SOURCE

    def __init__(
        self,
        symbols: list[str],
        fetch: Fetch,
        *,
        history_root: Path,
        clock: Callable[[], datetime] | None = None,
        slow_calls_per_poll: int = 2,
        **settings: Any,
    ) -> None:
        super().__init__(symbols, fetch, clock=clock, **settings)
        self.fixtures = BarStore(history_root)
        self.history: bool | None = None  # can this key read candles? None until probed
        self.is_open: bool | None = None
        self.status_at: datetime | None = None
        self.slow_calls_per_poll = slow_calls_per_poll
        self._samples: dict[str, list[tuple[datetime, float]]] = {s: [] for s in self.symbols}
        self._today: dict[str, tuple[float, float, float, float]] = {}
        self._slow_queue: list[tuple[str, str]] = []

    # --- polling --------------------------------------------------------------------

    async def refresh(self) -> None:
        """Market status, every quote, then a couple of slow calls. Never raises."""
        now = self.clock()
        if self.backoff_until and now < self.backoff_until:
            return
        errors: list[BaseException] = []
        try:
            await self._status(now)
        except Exception as exc:
            errors.append(exc)
        results = await asyncio.gather(*(self._quote(s, now) for s in self.symbols),
                                       return_exceptions=True)  # fmt: skip
        errors.extend(r for r in results if isinstance(r, BaseException))
        if not any(isinstance(e, RateLimitedError) for e in errors):
            for _ in range(self.slow_calls_per_poll):
                try:
                    await self._slow_call(now)
                except Exception as exc:
                    errors.append(exc)
        if any(isinstance(error, RateLimitedError) for error in errors):
            self.backoff_until = now + timedelta(minutes=1)
        self.last_error = f"{type(errors[0]).__name__}: {errors[0]}" if errors else None
        if errors:
            logger.warning("live prices: %d calls failed (%s)", len(errors), self.last_error)
        for symbol in self.symbols:
            self._rebuild(symbol, now)

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._limit:
            return await self._fetch(f"{BASE}{path}", params)

    async def _status(self, now: datetime) -> None:
        data = await self._get("/stock/market-status", {"exchange": "US"})
        session = data.get("session")
        self.is_open = bool(data.get("isOpen")) and session in (None, "regular")
        self.status_at = now
        if self.is_open:
            self.session = todays_session(now)
        else:
            start = next_regular_open(now)
            self.session = Session(start, start + timedelta(hours=6, minutes=30))

    async def _quote(self, symbol: str, now: datetime) -> None:
        data = await self._get("/quote", {"symbol": symbol})
        price = data.get("c")
        if not price:
            raise ValueError(f"no quote for {symbol}")
        slot = self.data[symbol]
        stamp = data.get("t")
        at = datetime.fromtimestamp(int(stamp), UTC) if stamp else now
        slot.quote = Quote(float(price), at)
        slot.previous = float(data["pc"]) if data.get("pc") else None
        slot.fetched_at = now
        if all(data.get(key) for key in ("o", "h", "l")):
            self._today[symbol] = (float(data["o"]), float(data["h"]), float(data["l"]),
                                   float(price))  # fmt: skip
        if self.market_open(now):
            samples = self._samples[symbol]
            samples.append((now, float(price)))
            today = now.astimezone(MARKET_TZ).date()
            self._samples[symbol] = [
                (t, p) for t, p in samples if t.astimezone(MARKET_TZ).date() == today
            ]

    async def _slow_call(self, now: datetime) -> None:
        """One call from the round robin: news for a symbol, or its price history."""
        if not self._slow_queue:
            kinds = ["news"] if self.history is False else ["news", "history"]
            self._slow_queue = [(kind, s) for s in self.symbols for kind in kinds]
        kind, symbol = self._slow_queue.pop(0)
        if kind == "news":
            self.data[symbol].news = await self._company_news(symbol, now)
            self.data[symbol].news_at = now
        else:
            await self._history(symbol, now)

    async def _company_news(self, symbol: str, now: datetime) -> list[Headline]:
        to = now.astimezone(MARKET_TZ).date()
        data = await self._get("/company-news", {"symbol": symbol,
                                                  "from": (to - timedelta(days=7)).isoformat(),
                                                  "to": to.isoformat()})  # fmt: skip
        items = []
        for raw in (data.get("items") or [])[:8]:
            if not raw.get("headline") or not raw.get("datetime"):
                continue
            related = [s.strip() for s in str(raw.get("related") or "").split(",")]
            items.append(Headline(
                id=str(raw.get("id") or raw["headline"])[:64],
                symbols=sorted({s for s in related if s in self.data} | {symbol}),
                published_at=datetime.fromtimestamp(int(raw["datetime"]), UTC),
                source=str(raw.get("source") or "news"),
                headline=str(raw["headline"])[:300],
            ))  # fmt: skip
        return items

    async def _history(self, symbol: str, now: datetime) -> None:
        to = int(now.timestamp())
        try:
            daily = await self._get("/stock/candle", {
                "symbol": symbol, "resolution": "D",
                "from": to - HISTORY_DAYS * 86400, "to": to})  # fmt: skip
            intraday = await self._get("/stock/candle", {
                "symbol": symbol, "resolution": "5",
                "from": to - INTRADAY_DAYS * 86400, "to": to})  # fmt: skip
        except AccessDeniedError:
            if self.history is None:
                logger.info("Finnhub: this key has no price history; building it from quotes")
            self.history = False
            return
        self.history = True
        slot = self.data[symbol]
        slot.daily = candle_bars(symbol, "1d", daily)
        slot.intraday = candle_bars(symbol, "5m", intraday)
        slot.daily_at = now

    def _rebuild(self, symbol: str, now: datetime) -> None:
        """Without history from Finnhub: bars from collected quotes and saved history."""
        if self.history:
            return
        slot = self.data[symbol]
        slot.intraday = bars_from_samples(symbol, self._samples[symbol])
        today = now.astimezone(MARKET_TZ).date()
        daily = [b for b in self.fixtures.all_bars(symbol, "1d")
                 if b.start.astimezone(MARKET_TZ).date() < today]  # fmt: skip
        live = self._today.get(symbol)
        quote = slot.quote
        if live and quote and quote.at.astimezone(MARKET_TZ).date() == today:
            start = daily_bar_start(today)
            o, h, low, c = live
            daily.append(Bar(symbol, start, start + INTERVALS["1d"], o, h, low, c, 0))
        slot.daily = daily

    # --- reading --------------------------------------------------------------------

    def market_open(self, now: datetime | None = None) -> bool:
        """Finnhub's own open flag, if it's fresh; unknown means closed (fail closed)."""
        moment = now or self.clock()
        if self.is_open is None or self.status_at is None:
            return False
        if moment - self.status_at > self.stale_after:
            return False
        return self.is_open
