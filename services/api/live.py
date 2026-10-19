"""Live prices for the trading desk (F10f), from Yahoo Finance's public chart endpoint.

No account and no key (user decision 2026-10-18). One shared `LiveFeed` polls each
symbol's chart (latest price, 5 minute bars, daily bars, the regular session times) and
recent headlines, and every trader's `LiveMarket` reads from it. The endpoint is
unofficial, so the feed is polite (a plain user agent, slow polling, back off on 429) and
fails closed: when prices are stale, orders are refused.

Orders never reach a real broker. A `LiveMarket` is the replay broker (services/
replay_server) with live data: the same MCP tools, cash account rules and guard.
"""

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from services.replay_server.bars import INTERVALS, MARKET_CLOSE, MARKET_OPEN, MARKET_TZ, Bar
from services.replay_server.broker import Order, Position, SimBroker, to_decimal
from services.replay_server.scenario import Scenario
from services.replay_server.session import (
    MAX_ADVANCE_MINUTES,
    MAX_NEWS,
    ReplaySession,
    ToolInputError,
)

logger = logging.getLogger(__name__)
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
# A browser-like user agent without a browser's TLS fingerprint gets 429; a plain one works.
USER_AGENT = "tradeguard/0.1 (local paper trading demo)"
SOURCE = "Yahoo Finance"

Fetch = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class RateLimitedError(Exception):
    """Yahoo answered 429."""


def http_fetch(client: httpx.AsyncClient) -> Fetch:
    async def fetch(url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT})
        if response.status_code == 429:
            raise RateLimitedError(url)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    return fetch


# --- parsing ---------------------------------------------------------------------------


def _in_session(start: datetime) -> bool:
    local = start.astimezone(MARKET_TZ).time()
    return MARKET_OPEN <= local < MARKET_CLOSE


def parse_bars(symbol: str, interval: str, result: dict[str, Any]) -> list[Bar]:
    """Bars from a chart result, regular session only, skipping empty rows. Yahoo's
    timestamps are bar starts (daily bars start at the 09:30 open)."""
    stamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    length = INTERVALS[interval]
    bars = []
    for index, stamp in enumerate(stamps):
        values = [quote.get(key, [None] * len(stamps))[index]
                  for key in ("open", "high", "low", "close")]  # fmt: skip
        if any(value is None for value in values):
            continue
        start = datetime.fromtimestamp(stamp, UTC)
        if interval == "5m" and not _in_session(start):
            continue  # Yahoo adds a zero length 16:00 print
        volume = (quote.get("volume") or [0] * len(stamps))[index] or 0
        o, h, low, c = (round(float(v), 4) for v in values)
        bars.append(Bar(symbol, start, start + length, o, h, low, c, int(volume)))
    return bars


@dataclass
class Quote:
    price: float
    at: datetime


@dataclass
class Session:
    """A regular trading session, from Yahoo's `currentTradingPeriod` (holidays included)."""

    start: datetime
    end: datetime


@dataclass
class Headline:
    id: str
    symbols: list[str]
    published_at: datetime
    source: str
    headline: str

    def public_view(self) -> dict[str, Any]:
        return {"id": self.id, "symbols": self.symbols,
                "published_at": self.published_at.isoformat(), "source": self.source,
                "headline": self.headline, "body": ""}  # fmt: skip


@dataclass
class SymbolData:
    quote: Quote | None = None
    intraday: list[Bar] = field(default_factory=list)
    daily: list[Bar] = field(default_factory=list)
    daily_at: datetime | None = None
    fetched_at: datetime | None = None
    news: list[Headline] = field(default_factory=list)
    news_at: datetime | None = None


# --- the shared feed -------------------------------------------------------------------


class LiveFeed:
    """Latest prices, bars, session times and headlines for a fixed set of symbols."""

    def __init__(
        self,
        symbols: list[str],
        fetch: Fetch,
        *,
        clock: Callable[[], datetime] | None = None,
        stale_after_s: float = 180,
        daily_every_s: float = 1800,
        news_every_s: float = 600,
        concurrency: int = 4,
    ) -> None:
        self.symbols = sorted({symbol.upper() for symbol in symbols})
        self._fetch = fetch
        self.clock = clock or (lambda: datetime.now(UTC))
        self.stale_after = timedelta(seconds=stale_after_s)
        self.daily_every = timedelta(seconds=daily_every_s)
        self.news_every = timedelta(seconds=news_every_s)
        self._limit = asyncio.Semaphore(concurrency)
        self.data: dict[str, SymbolData] = {symbol: SymbolData() for symbol in self.symbols}
        self.session: Session | None = None
        self.last_error: str | None = None
        self.backoff_until: datetime | None = None

    # --- polling ------------------------------------------------------------------

    async def refresh(self) -> None:
        """Fetch every symbol once (daily bars and news only when due). Never raises."""
        now = self.clock()
        if self.backoff_until and now < self.backoff_until:
            return
        results = await asyncio.gather(*(self._refresh_symbol(s, now) for s in self.symbols),
                                       return_exceptions=True)  # fmt: skip
        errors = [r for r in results if isinstance(r, BaseException)]
        if any(isinstance(error, RateLimitedError) for error in errors):
            self.backoff_until = now + timedelta(minutes=2)
        self.last_error = f"{type(errors[0]).__name__}: {errors[0]}" if errors else None
        if errors:
            logger.warning("live prices: %d of %d symbols failed (%s)", len(errors),
                           len(self.symbols), self.last_error)  # fmt: skip

    async def _chart(self, symbol: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._limit:
            data = await self._fetch(CHART_URL.format(symbol=symbol), params)
        result: dict[str, Any] = data["chart"]["result"][0]
        return result

    async def _refresh_symbol(self, symbol: str, now: datetime) -> None:
        slot = self.data[symbol]
        result = await self._chart(symbol, {"interval": "5m", "range": "5d"})
        meta = result.get("meta", {})
        slot.intraday = parse_bars(symbol, "5m", result)
        price, stamp = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
        if price is not None and stamp:
            slot.quote = Quote(float(price), datetime.fromtimestamp(int(stamp), UTC))
        regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        if regular.get("start") and regular.get("end"):
            self.session = Session(datetime.fromtimestamp(int(regular["start"]), UTC),
                                   datetime.fromtimestamp(int(regular["end"]), UTC))  # fmt: skip
        slot.fetched_at = now
        if slot.daily_at is None or now - slot.daily_at >= self.daily_every:
            daily = await self._chart(symbol, {"interval": "1d", "range": "1y"})
            slot.daily = parse_bars(symbol, "1d", daily)
            slot.daily_at = now
        if slot.news_at is None or now - slot.news_at >= self.news_every:
            slot.news = await self._news(symbol)
            slot.news_at = now

    async def _news(self, symbol: str) -> list[Headline]:
        async with self._limit:
            data = await self._fetch(SEARCH_URL, {"q": symbol, "newsCount": 8, "quotesCount": 0})
        items = []
        for raw in data.get("news") or []:
            if not raw.get("title") or not raw.get("providerPublishTime"):
                continue
            related = [s for s in raw.get("relatedTickers") or [] if s in self.data]
            items.append(Headline(
                id=str(raw.get("uuid") or raw["title"])[:64],
                symbols=sorted(set(related) | {symbol}),
                published_at=datetime.fromtimestamp(int(raw["providerPublishTime"]), UTC),
                source=str(raw.get("publisher") or "news"),
                headline=str(raw["title"])[:300],
            ))  # fmt: skip
        return items

    # --- reading ------------------------------------------------------------------

    def market_open(self, now: datetime | None = None) -> bool:
        """Open only inside Yahoo's current regular session. Unknown means closed."""
        moment = now or self.clock()
        return self.session is not None and self.session.start <= moment < self.session.end

    def stale(self, symbol: str, now: datetime | None = None) -> bool:
        slot = self.data.get(symbol)
        if slot is None or slot.quote is None or slot.fetched_at is None:
            return True
        return (now or self.clock()) - slot.fetched_at > self.stale_after

    def quote(self, symbol: str) -> Quote | None:
        slot = self.data.get(symbol)
        return slot.quote if slot else None

    def all_bars(self, symbol: str, interval: str) -> list[Bar]:
        slot = self.data.get(symbol)
        if slot is None:
            return []
        return list(slot.intraday if interval == "5m" else slot.daily)

    def previous_close(self, symbol: str) -> float | None:
        """The close of the last session before the latest quote's session."""
        slot = self.data.get(symbol)
        if slot is None or slot.quote is None:
            return None
        quote_day = slot.quote.at.astimezone(MARKET_TZ).date()
        for bar in reversed(slot.daily):
            if bar.start.astimezone(MARKET_TZ).date() < quote_day:
                return bar.close
        return None

    def headlines(self, symbol: str | None = None) -> list[Headline]:
        seen: dict[str, Headline] = {}
        for name, slot in self.data.items():
            if symbol is not None and name != symbol:
                continue
            for item in slot.news:
                seen.setdefault(item.id, item)
        return sorted(seen.values(), key=lambda item: item.published_at, reverse=True)

    def status(self) -> dict[str, Any]:
        now = self.clock()
        times = [slot.quote.at for slot in self.data.values() if slot.quote is not None]
        fetched = [slot.fetched_at for slot in self.data.values() if slot.fetched_at]
        return {
            "live": True,
            "source": SOURCE,
            "now": now.isoformat(),
            "market_open": self.market_open(now),
            "session_start": self.session.start.isoformat() if self.session else None,
            "session_end": self.session.end.isoformat() if self.session else None,
            "prices_as_of": max(times).isoformat() if times else None,
            "updated_at": max(fetched).isoformat() if fetched else None,
            "stale": not fetched or now - max(fetched) > self.stale_after,
        }


# --- one trader's market ---------------------------------------------------------------


class LiveBroker(SimBroker):
    """Random order ids: live fills can't be replayed, so ids needn't be reproducible."""

    def new_order_id(self) -> str:
        return f"ord_{secrets.token_hex(6)}"


class SavedBook:
    """A market whose book (cash, positions, orders) can be saved and loaded, so a trader's
    account survives the API restarting (F10f, F12)."""

    broker: SimBroker

    def snapshot(self) -> dict[str, Any]:
        broker = self.broker
        return {
            "cash": str(broker.cash),
            "positions": {symbol: {"qty": str(p.qty), "avg": str(p.avg_entry_price)}
                          for symbol, p in broker.positions.items()},
            "orders": [_order_state(order) for order in broker.orders],
        }  # fmt: skip

    def load(self, book: dict[str, Any]) -> None:
        broker = self.broker
        broker.cash = Decimal(book["cash"])
        broker.positions = {symbol: Position(Decimal(p["qty"]), Decimal(p["avg"]))
                            for symbol, p in book.get("positions", {}).items()}  # fmt: skip
        broker.orders = [_order_from(state) for state in book.get("orders", [])]


class DeskReplayMarket(SavedBook, ReplaySession):
    """The trading desk on replayed prices (LIVE_PRICES=false, F12): the replay market with
    a saved book and random order ids, so a loaded account never reuses an id."""

    def __init__(self, scenario: Scenario, *, bars_root: Path, news_root: Path) -> None:
        super().__init__(scenario, bars_root=bars_root, news_root=news_root)
        self.broker = LiveBroker(cash=self.broker.cash, seed=self.broker.seed)

    def load(self, book: dict[str, Any]) -> None:
        """Load the book, then move the clock past its latest order so fills line up."""
        super().load(book)
        times = []
        for order in self.broker.orders:
            times.append(order.submitted_at)
            if order.filled_at is not None:
                times.append(order.filled_at)
        if times and max(times) > self.now:
            minutes = int((max(times) - self.now).total_seconds() // 60) + 1
            while minutes > 0:
                step = min(minutes, MAX_ADVANCE_MINUTES)
                self.advance(step)
                minutes -= step


class LiveMarket(SavedBook, ReplaySession):
    """A trader's paper account priced by the live feed. Same tools as a replay session.

    Market orders fill at the live price while the market is open; limit orders fill when
    the live price reaches the limit; day orders expire at the close. With stale prices,
    new orders are refused (fail closed).
    """

    live = True

    def __init__(self, scenario: Scenario, feed: LiveFeed, *, cash: float) -> None:
        # Not ReplaySession.__init__: no fixtures. The fields its methods use are set here.
        self.scenario = scenario
        self.feed = feed
        self.store = feed  # type: ignore[assignment]  # same all_bars/symbols reads
        self.news = []
        self.known_symbols = set(feed.symbols)
        self.broker = LiveBroker(cash=to_decimal(cash), seed=0)

    @property
    def now(self) -> datetime:  # type: ignore[override]
        return self.feed.clock()

    def advance(self, minutes: int) -> dict[str, Any]:
        raise ToolInputError("a live market's clock can't be moved")

    def get_clock(self) -> dict[str, Any]:
        return {"now": self.now.isoformat(), "market_open": self.feed.market_open(self.now)}

    def get_quote(self, symbol: str) -> dict[str, Any]:
        symbol = self._symbol(symbol)
        price, as_of = self._price_now(symbol)
        return {"symbol": symbol, "price": float(price), "as_of": as_of.isoformat(),
                "market_open": self.feed.market_open(self.now)}  # fmt: skip

    def get_news(self, symbol: str | None = None, limit: int = 10) -> dict[str, Any]:
        """Real recent headlines. Untrusted external text, like any news feed."""
        if limit < 1 or limit > MAX_NEWS:
            raise ToolInputError(f"limit must be between 1 and {MAX_NEWS}")
        wanted = self._symbol(symbol) if symbol else None
        items = [item.public_view() for item in self.feed.headlines(wanted)[:limit]]
        return {"as_of": self.now.isoformat(), "items": items}

    def get_account(self) -> dict[str, Any]:
        account = super().get_account()
        return {**account, "account_id": "paper-live", "mode": "paper (live prices)"}

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float | None = None,
        notional: float | None = None,
        order_type: str = "market",
        limit_price: float | None = None,
    ) -> dict[str, Any]:
        checked = self._symbol(symbol)
        if self.feed.stale(checked, self.now):
            raise ToolInputError(
                f"live prices for {checked} are unavailable right now, so orders are paused"
            )
        order = super().place_order(symbol, side, qty, notional, order_type, limit_price)
        self._process()  # a market order fills now, at the live price
        for current in self.broker.orders:
            if current.id == order["id"]:
                return current.to_dict()
        return order

    def previous_close(self, symbol: str) -> float | None:
        return self.feed.previous_close(symbol)

    def settle(self) -> bool:
        """Fill or expire open orders at the latest prices. True when anything changed."""
        before = [order.status for order in self.broker.orders]
        self._process()
        return before != [order.status for order in self.broker.orders]

    def _price_now(self, symbol: str) -> tuple[Decimal, datetime]:
        quote = self.feed.quote(symbol)
        if quote is None:
            raise ToolInputError(f"no live price for {symbol} yet; try again in a minute")
        return to_decimal(quote.price), quote.at

    def _process(self) -> None:
        now = self.now
        open_now = self.feed.market_open(now)
        for order in self.broker.orders:
            if order.status != "new":
                continue
            quote = self.feed.quote(order.symbol)
            if open_now and quote is not None and not self.feed.stale(order.symbol, now):
                price = to_decimal(quote.price)
                limit = order.limit_price
                if (
                    order.type == "market"
                    or (order.side == "buy" and limit is not None and price <= limit)
                    or (order.side == "sell" and limit is not None and price >= limit)
                ):
                    self.broker._execute(order, price, now)
            if order.status == "new" and now >= order.expires_at:
                order.status = "expired"


_PLAIN = ("id", "symbol", "side", "type", "status", "reject_reason")
_DECIMALS = ("qty", "notional", "limit_price", "reserved", "filled_qty", "filled_avg_price")
_TIMES = ("submitted_at", "expires_at", "filled_at")


def _order_state(order: Order) -> dict[str, Any]:
    state: dict[str, Any] = {"id": order.id, "symbol": order.symbol, "side": order.side,
                             "type": order.type, "status": order.status,
                             "reject_reason": order.reject_reason}  # fmt: skip
    for name in _DECIMALS:
        value = getattr(order, name)
        state[name] = None if value is None else str(value)
    for name in _TIMES:
        moment = getattr(order, name)
        state[name] = None if moment is None else moment.isoformat()
    return state


def _order_from(state: dict[str, Any]) -> Order:
    values: dict[str, Any] = {key: state[key] for key in _PLAIN}
    for name in _DECIMALS:
        values[name] = None if state.get(name) is None else Decimal(state[name])
    for name in _TIMES:
        values[name] = None if state.get(name) is None else datetime.fromisoformat(state[name])
    return Order(**values)
