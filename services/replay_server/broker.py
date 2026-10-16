"""Simulated cash account broker.

Fill rules (all in UTC, against 5 minute bars):
- Orders can only fill on bars that start strictly after submission and before expiry.
- Market: fills at the open of the first such bar, once that bar has started.
- Limit: fills at the open if the open is at or through the limit. Otherwise it fills at the
  limit price if the bar's range touches it, but only once that bar has closed, because the
  bar's high and low are unknown until then.
- Day orders expire at the next 16:00 New York close.
- Cash account: no shorting, no margin. Open orders reserve cash or shares.
"""

import random
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

from services.replay_server.bars import MARKET_CLOSE, MARKET_TZ, Bar

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
Status = Literal["new", "filled", "canceled", "rejected", "expired"]

CENT = Decimal("0.01")
SHARE_STEP = Decimal("0.000001")


@dataclass
class Order:
    id: str
    symbol: str
    side: Side
    type: OrderType
    qty: Decimal | None
    notional: Decimal | None
    limit_price: Decimal | None
    submitted_at: datetime
    expires_at: datetime
    reserved: Decimal  # cash for buys, shares for sells
    status: Status = "new"
    filled_at: datetime | None = None
    filled_qty: Decimal | None = None
    filled_avg_price: Decimal | None = None
    reject_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "side": self.side,
            "type": self.type,
            "qty": to_float(self.qty),
            "notional": to_float(self.notional),
            "limit_price": to_float(self.limit_price),
            "status": self.status,
            "submitted_at": self.submitted_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "filled_qty": to_float(self.filled_qty),
            "filled_avg_price": to_float(self.filled_avg_price),
            "reject_reason": self.reject_reason,
        }


@dataclass
class Position:
    qty: Decimal
    avg_entry_price: Decimal


@dataclass
class SimBroker:
    cash: Decimal
    seed: int
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Ids come from the scenario seed so repeated runs are byte for byte identical.
        self._rng = random.Random(self.seed)  # noqa: S311 - not security sensitive

    def new_order_id(self) -> str:
        return f"ord_{self._rng.getrandbits(48):012x}"

    def reserved_cash(self) -> Decimal:
        total = Decimal(0)
        for order in self.orders:
            if order.status == "new" and order.side == "buy":
                total += order.reserved
        return total

    def reserved_shares(self, symbol: str) -> Decimal:
        total = Decimal(0)
        for order in self.orders:
            if order.status == "new" and order.side == "sell" and order.symbol == symbol:
                total += order.reserved
        return total

    def buying_power(self) -> Decimal:
        return self.cash - self.reserved_cash()

    def held(self, symbol: str) -> Decimal:
        position = self.positions.get(symbol)
        return position.qty if position else Decimal(0)

    def submit(
        self,
        *,
        symbol: str,
        side: Side,
        order_type: OrderType,
        qty: Decimal | None,
        notional: Decimal | None,
        limit_price: Decimal | None,
        reference_price: Decimal,
        now: datetime,
    ) -> Order:
        """Record a new order, or a rejected one if cash or shares are short.

        Arguments are assumed valid (the session checks them). Returns the stored order.
        """
        order = Order(
            id=self.new_order_id(),
            symbol=symbol,
            side=side,
            type=order_type,
            qty=qty,
            notional=notional,
            limit_price=limit_price,
            submitted_at=now,
            expires_at=next_close(now),
            reserved=Decimal(0),
        )
        price = limit_price if limit_price is not None else reference_price
        if side == "buy":
            cost = notional if notional is not None else (qty or Decimal(0)) * price
            if cost > self.buying_power():
                order.status = "rejected"
                order.reject_reason = (
                    f"insufficient buying power: needs about {cost.quantize(CENT)}, "
                    f"available {self.buying_power().quantize(CENT)}"
                )
            else:
                order.reserved = cost
        else:
            shares = qty if qty is not None else (notional or Decimal(0)) / price
            available = self.held(symbol) - self.reserved_shares(symbol)
            if shares > available:
                order.status = "rejected"
                order.reject_reason = (
                    f"insufficient position: selling {shares.normalize()} of {symbol} but "
                    f"only {available.normalize()} available (cash account, no shorting)"
                )
            else:
                order.reserved = shares
        self.orders.append(order)
        return order

    def cancel(self, order: Order) -> None:
        if order.status == "new":
            order.status = "canceled"

    def process(self, bars_by_symbol: dict[str, list[Bar]], now: datetime) -> None:
        """Fill or expire open orders using only bar information known at `now`."""
        for order in self.orders:
            if order.status != "new":
                continue
            self._try_fill(order, bars_by_symbol.get(order.symbol, []), now)
            if order.status == "new" and now >= order.expires_at:
                order.status = "expired"

    def _try_fill(self, order: Order, bars: list[Bar], now: datetime) -> None:
        # Strictly after submission: an order sent at 15:00:00 fills at the 15:05 open.
        index = bisect_right(bars, order.submitted_at, key=bar_start)
        while index < len(bars):
            bar = bars[index]
            if bar.start >= order.expires_at or bar.start > now:
                return
            fill = fill_price(order, bar, now)
            if fill is not None:
                price, at = fill
                self._execute(order, price, at)
                return
            if bar.end > now:
                return  # this bar's range is still unknown
            index += 1

    def _execute(self, order: Order, price: Decimal, at: datetime) -> None:
        if order.qty is not None:
            qty = order.qty
        else:
            qty = ((order.notional or Decimal(0)) / price).quantize(SHARE_STEP, ROUND_DOWN)
        value = qty * price
        if order.side == "buy":
            if value > self.cash:
                order.status = "rejected"
                order.reject_reason = "insufficient buying power at fill"
                return
            self.cash -= value
            position = self.positions.get(order.symbol)
            if position is None:
                self.positions[order.symbol] = Position(qty=qty, avg_entry_price=price)
            else:
                total = position.qty + qty
                position.avg_entry_price = (position.avg_entry_price * position.qty + value) / total
                position.qty = total
        else:
            held = self.held(order.symbol)
            if qty > held:
                order.status = "rejected"
                order.reject_reason = "insufficient position at fill"
                return
            self.cash += value
            remaining = held - qty
            if remaining == 0:
                del self.positions[order.symbol]
            else:
                self.positions[order.symbol].qty = remaining
        order.status = "filled"
        order.filled_at = at
        order.filled_qty = qty
        order.filled_avg_price = price


def fill_price(order: Order, bar: Bar, now: datetime) -> tuple[Decimal, datetime] | None:
    """Price and time this bar fills the order at, or None if it doesn't (yet)."""
    open_price = to_decimal(bar.open)
    if order.type == "market":
        return open_price, bar.start

    limit = order.limit_price
    if limit is None:
        return None
    if order.side == "buy" and open_price <= limit:
        return open_price, bar.start
    if order.side == "sell" and open_price >= limit:
        return open_price, bar.start
    if bar.end > now:
        return None
    if order.side == "buy" and to_decimal(bar.low) <= limit:
        return limit, bar.end
    if order.side == "sell" and to_decimal(bar.high) >= limit:
        return limit, bar.end
    return None


def next_close(moment: datetime) -> datetime:
    """The next 16:00 New York close at or after `moment`, skipping weekends."""
    local = moment.astimezone(MARKET_TZ)
    day = local.date()
    if local.time() >= MARKET_CLOSE:
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    close = datetime.combine(day, MARKET_CLOSE, tzinfo=MARKET_TZ)
    return close.astimezone(moment.tzinfo)


def bar_start(bar: Bar) -> datetime:
    return bar.start


def to_decimal(value: float) -> Decimal:
    return Decimal(str(value))


def to_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)
