"""One replay session: a simulated clock, the sim broker, and read only market data.

Every public method is one MCP tool. Each returns a JSON-ready dict and never reveals data
from after the session's current time.
"""

from bisect import bisect_right
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from services.replay_server.bars import INTERVALS, Bar, BarStore, bar_start, is_market_open
from services.replay_server.broker import OrderType, Position, Side, SimBroker, to_decimal
from services.replay_server.news import NewsItem, load_news
from services.replay_server.scenario import Scenario

MAX_BARS = 500
MAX_NEWS = 100
MAX_ADVANCE_MINUTES = 60 * 24 * 7


class ToolInputError(ValueError):
    """The tool arguments are invalid. The message is safe to show to the agent."""


class ReplaySession:
    def __init__(self, scenario: Scenario, *, bars_root: Path, news_root: Path) -> None:
        """Start a session at the scenario's start time.

        Raises ToolInputError if a starting position has no price at the start time, and
        FileNotFoundError if a listed news fixture is missing.
        """
        self.scenario = scenario
        self.now = scenario.start_time
        self.store = BarStore(bars_root)
        self.news: list[NewsItem] = load_news(news_root, scenario.news_fixtures)
        self.known_symbols = set(self.store.symbols("1d")) | set(self.store.symbols("5m"))
        self.broker = SimBroker(cash=to_decimal(scenario.starting_cash), seed=scenario.seed)
        for symbol, qty in scenario.positions.items():
            price = self._price_now(self._symbol(symbol))[0]
            self.broker.positions[symbol.upper()] = Position(
                qty=Decimal(str(qty)), avg_entry_price=price
            )

    # --- clock ----------------------------------------------------------------------

    def advance(self, minutes: int) -> dict[str, Any]:
        """Move the clock forward and process fills and expiries. Not exposed to the agent."""
        if minutes <= 0 or minutes > MAX_ADVANCE_MINUTES:
            raise ToolInputError(f"minutes must be between 1 and {MAX_ADVANCE_MINUTES}")
        self.now = self.now + timedelta(minutes=minutes)
        self._process()
        return self.get_clock()

    def get_clock(self) -> dict[str, Any]:
        """Current simulated time and whether the regular session is open."""
        return {"now": self.now.isoformat(), "market_open": is_market_open(self.now)}

    # --- market data ----------------------------------------------------------------

    def get_bars(
        self, symbol: str, interval: str = "1d", limit: int = 50, end: str | None = None
    ) -> dict[str, Any]:
        """Most recent fully closed bars, oldest first, ending no later than now."""
        symbol = self._symbol(symbol)
        if interval not in INTERVALS:
            raise ToolInputError(f"interval must be one of {sorted(INTERVALS)}")
        if limit < 1 or limit > MAX_BARS:
            raise ToolInputError(f"limit must be between 1 and {MAX_BARS}")
        cutoff = self.now
        if end is not None:
            requested = _parse_time(end)
            if requested < cutoff:
                cutoff = requested
        visible = _closed_bars(self.store.all_bars(symbol, interval), cutoff)
        rows = []
        for bar in visible[-limit:]:
            rows.append(bar.to_dict())
        return {"symbol": symbol, "interval": interval, "as_of": self.now.isoformat(),
                "bars": rows}  # fmt: skip

    def get_quote(self, symbol: str) -> dict[str, Any]:
        """Latest known trade price: the open of the 5m bar in progress, else the last close."""
        symbol = self._symbol(symbol)
        price, as_of = self._price_now(symbol)
        return {
            "symbol": symbol,
            "price": float(price),
            "as_of": as_of.isoformat(),
            "market_open": is_market_open(self.now),
        }

    def get_news(self, symbol: str | None = None, limit: int = 10) -> dict[str, Any]:
        """Published news, newest first. Every item is untrusted external text."""
        if limit < 1 or limit > MAX_NEWS:
            raise ToolInputError(f"limit must be between 1 and {MAX_NEWS}")
        wanted = self._symbol(symbol) if symbol else None
        visible = []
        for item in self.news:
            if item.published_at > self.now:
                continue
            if wanted is not None and wanted not in item.symbols:
                continue
            visible.append(item)
        visible.sort(key=_published, reverse=True)
        rows = []
        for item in visible[:limit]:
            rows.append(item.public_view())
        return {"as_of": self.now.isoformat(), "items": rows}

    # --- account --------------------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        self._process()
        positions_value = Decimal(0)
        for symbol, position in self.broker.positions.items():
            positions_value += position.qty * self._price_now(symbol)[0]
        return {
            "account_id": f"sim-{self.scenario.id}",
            "mode": "replay",
            "currency": "USD",
            "cash": _money(self.broker.cash),
            "buying_power": _money(self.broker.buying_power()),
            "positions_value": _money(positions_value),
            "equity": _money(self.broker.cash + positions_value),
        }

    def get_positions(self) -> dict[str, Any]:
        self._process()
        rows = []
        for symbol in sorted(self.broker.positions):
            position = self.broker.positions[symbol]
            price = self._price_now(symbol)[0]
            value = position.qty * price
            rows.append(
                {
                    "symbol": symbol,
                    "qty": float(position.qty),
                    "avg_entry_price": _money(position.avg_entry_price),
                    "market_price": float(price),
                    "market_value": _money(value),
                    "unrealized_pl": _money(value - position.qty * position.avg_entry_price),
                }
            )
        return {"positions": rows}

    # --- orders ---------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float | None = None,
        notional: float | None = None,
        order_type: str = "market",
        limit_price: float | None = None,
    ) -> dict[str, Any]:
        """Submit a day order. Returns the order; broker refusals come back as status rejected."""
        symbol = self._symbol(symbol)
        checked_side = _check_side(side)
        checked_type = _check_type(order_type)
        if (qty is None) == (notional is None):
            raise ToolInputError("give exactly one of qty (shares) or notional (dollars)")
        qty_d = _positive("qty", qty)
        notional_d = _positive("notional", notional)
        limit_d = _positive("limit_price", limit_price)
        if checked_type == "limit" and limit_d is None:
            raise ToolInputError("limit orders need limit_price")
        if checked_type == "market" and limit_d is not None:
            raise ToolInputError("market orders do not take limit_price")
        if checked_type == "limit" and notional_d is not None:
            raise ToolInputError("limit orders must use qty, not notional")

        self._process()
        order = self.broker.submit(
            symbol=symbol,
            side=checked_side,
            order_type=checked_type,
            qty=qty_d,
            notional=notional_d,
            limit_price=limit_d,
            reference_price=self._price_now(symbol)[0],
            now=self.now,
        )
        return order.to_dict()

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        self._process()
        for order in self.broker.orders:
            if order.id == order_id:
                if order.status != "new":
                    raise ToolInputError(f"order {order_id} is {order.status}, not open")
                self.broker.cancel(order)
                return order.to_dict()
        raise ToolInputError(f"no order with id {order_id}")

    def list_orders(self, status: str = "all") -> dict[str, Any]:
        """Orders oldest first. status: all, open (still working) or closed."""
        if status not in ("all", "open", "closed"):
            raise ToolInputError("status must be all, open or closed")
        self._process()
        rows = []
        for order in self.broker.orders:
            is_open = order.status == "new"
            if status == "open" and not is_open:
                continue
            if status == "closed" and is_open:
                continue
            rows.append(order.to_dict())
        return {"orders": rows}

    # --- helpers --------------------------------------------------------------------

    def _symbol(self, symbol: str) -> str:
        cleaned = symbol.strip().upper()
        if cleaned not in self.known_symbols:
            raise ToolInputError(f"unknown symbol {cleaned}")
        return cleaned

    def _price_now(self, symbol: str) -> tuple[Decimal, datetime]:
        intraday = self.store.all_bars(symbol, "5m")
        started = intraday[: bisect_right(intraday, self.now, key=bar_start)]
        candidates: list[tuple[datetime, float]] = []
        if started:
            last = started[-1]
            if last.end > self.now:
                candidates.append((last.start, last.open))
            else:
                candidates.append((last.end, last.close))
        daily = _closed_bars(self.store.all_bars(symbol, "1d"), self.now)
        if daily:
            candidates.append((daily[-1].end, daily[-1].close))
        if not candidates:
            raise ToolInputError(f"no price for {symbol} at {self.now.isoformat()}")
        as_of, price = max(candidates)
        return to_decimal(price), as_of

    def _process(self) -> None:
        bars_by_symbol: dict[str, list[Bar]] = {}
        for order in self.broker.orders:
            if order.symbol not in bars_by_symbol:
                bars_by_symbol[order.symbol] = self.store.all_bars(order.symbol, "5m")
        self.broker.process(bars_by_symbol, self.now)


def _closed_bars(bars: list[Bar], cutoff: datetime) -> list[Bar]:
    """Bars whose end is at or before cutoff. Bars are sorted by start and equal length."""
    started = bars[: bisect_right(bars, cutoff, key=bar_start)]
    if started and started[-1].end > cutoff:
        return started[:-1]
    return started


def _parse_time(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolInputError(f"end must be an ISO 8601 timestamp, got {text!r}") from exc
    if moment.tzinfo is None:
        raise ToolInputError("end needs a timezone, for example 2026-09-15T14:30:00Z")
    return moment


def _check_side(side: str) -> Side:
    if side == "buy":
        return "buy"
    if side == "sell":
        return "sell"
    raise ToolInputError("side must be buy or sell")


def _check_type(order_type: str) -> OrderType:
    if order_type == "market":
        return "market"
    if order_type == "limit":
        return "limit"
    raise ToolInputError("order_type must be market or limit")


def _positive(name: str, value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ToolInputError(f"{name} must be a number") from exc
    if not number.is_finite() or number <= 0:
        raise ToolInputError(f"{name} must be greater than 0")
    return number


def _published(item: NewsItem) -> datetime:
    return item.published_at


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))
