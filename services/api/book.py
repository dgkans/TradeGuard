"""A trader's replay market that remembers its orders, so it can be rebuilt (F10e).

The broker is deterministic: given the same starting cash, seed, orders and market times,
it ends in the same state (cash, positions, fills, order ids). So instead of serializing
the broker, a trader's account is stored as its order history; after a restart the market
is rebuilt by replaying that history at the recorded times and then catching up to the
shared desk clock.
"""

from datetime import datetime
from typing import Any

from services.replay_server.session import MAX_ADVANCE_MINUTES, ReplaySession


class RecordingMarket(ReplaySession):
    """A ReplaySession that appends every order and cancel that reached the broker."""

    events: list[dict[str, Any]]
    recording: bool

    def start_recording(self) -> None:
        self.events = []
        self.recording = True

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float | None = None,
        notional: float | None = None,
        order_type: str = "market",
        limit_price: float | None = None,
    ) -> dict[str, Any]:
        order = super().place_order(symbol, side, qty, notional, order_type, limit_price)
        if getattr(self, "recording", False):
            self.events.append({
                "kind": "place", "at": self.now.isoformat(), "order_id": order["id"],
                "args": {"symbol": symbol, "side": side, "qty": qty, "notional": notional,
                         "order_type": order_type, "limit_price": limit_price},
            })  # fmt: skip
        return order

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        result = super().cancel_order(order_id)
        if getattr(self, "recording", False):
            self.events.append({"kind": "cancel", "at": self.now.isoformat(),
                                "order_id": order_id})  # fmt: skip
        return result


def catch_up(market: ReplaySession, target: datetime) -> None:
    """Advance the market to `target` (no-op if it is already there or later)."""
    minutes = int((target - market.now).total_seconds() // 60)
    while minutes > 0:
        step = min(minutes, MAX_ADVANCE_MINUTES)
        market.advance(step)
        minutes -= step


def replay(market: RecordingMarket, events: list[dict[str, Any]]) -> None:
    """Rebuild an account from its order history. Raises if the history doesn't reproduce
    (a different order id means the data or the broker changed: fail loudly, don't guess)."""
    market.recording = False
    try:
        for event in events:
            catch_up(market, datetime.fromisoformat(str(event["at"])))
            if event["kind"] == "place":
                order = market.place_order(**event["args"])
                if order["id"] != event["order_id"]:
                    raise RuntimeError(
                        f"order history did not reproduce: {order['id']} != {event['order_id']}"
                    )
            elif event["kind"] == "cancel":
                market.cancel_order(str(event["order_id"]))
    finally:
        market.recording = True
    market.events = list(events)
