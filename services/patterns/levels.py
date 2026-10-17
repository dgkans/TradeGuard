"""Support and resistance from clustered swing pivots."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from services.patterns.types import OHLCV

PIVOT_SPAN = 2  # bars required on each side of a swing high or low
ATR_TOLERANCE = 0.5  # pivots within 0.5 ATR of each other form one level
FALLBACK_TOLERANCE_PCT = 1.0  # used when no ATR is available
MAX_LEVELS = 3

RULE = (
    "levels: swing high = high above the highs of the 2 bars on each side; swing low = low "
    "below the lows of the 2 bars on each side; pivots within 0.5 ATR are merged into one "
    "level (mean price); levels above the last close are resistance, others support"
)


@dataclass(frozen=True)
class Level:
    price: float
    touches: int
    pivot_starts: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": round(self.price, 4),
            "touches": self.touches,
            "pivot_starts": list(self.pivot_starts),
        }


@dataclass(frozen=True)
class Levels:
    support: list[Level]
    resistance: list[Level]

    def to_dict(self) -> dict[str, Any]:
        support = []
        for level in self.support:
            support.append(level.to_dict())
        resistance = []
        for level in self.resistance:
            resistance.append(level.to_dict())
        return {"rule": RULE, "support": support, "resistance": resistance}


def swing_pivots(bars: Sequence[OHLCV]) -> list[tuple[float, OHLCV]]:
    """(price, bar) for every confirmed swing high and swing low, in time order.

    A pivot needs PIVOT_SPAN bars after it, so the newest bars can never be pivots.
    """
    pivots = []
    for i in range(PIVOT_SPAN, len(bars) - PIVOT_SPAN):
        neighbours = list(bars[i - PIVOT_SPAN : i]) + list(bars[i + 1 : i + 1 + PIVOT_SPAN])
        bar = bars[i]
        if all(bar.high > other.high for other in neighbours):
            pivots.append((bar.high, bar))
        if all(bar.low < other.low for other in neighbours):
            pivots.append((bar.low, bar))
    return pivots


def support_resistance(bars: Sequence[OHLCV], atr_value: float | None) -> Levels:
    """Nearest support (below the last close) and resistance (above it), up to 3 each.

    Support is ordered nearest first (highest price), resistance nearest first (lowest).
    Returns empty lists when there are no confirmed pivots.
    """
    if not bars:
        return Levels(support=[], resistance=[])
    last_close = bars[-1].close
    if atr_value is not None and atr_value > 0:
        tolerance = ATR_TOLERANCE * atr_value
    else:
        tolerance = last_close * FALLBACK_TOLERANCE_PCT / 100

    levels = cluster(swing_pivots(bars), tolerance)
    support = []
    resistance = []
    for level in levels:
        if level.price > last_close:
            resistance.append(level)
        else:
            support.append(level)
    support.sort(key=level_price, reverse=True)
    resistance.sort(key=level_price)
    return Levels(support=support[:MAX_LEVELS], resistance=resistance[:MAX_LEVELS])


def cluster(pivots: list[tuple[float, OHLCV]], tolerance: float) -> list[Level]:
    """Group pivots by price: a pivot joins the current group while it is within
    `tolerance` of the group's lowest price."""
    ordered = sorted(pivots, key=pivot_price)
    groups: list[list[tuple[float, OHLCV]]] = []
    for pivot in ordered:
        if groups and pivot[0] - groups[-1][0][0] <= tolerance:
            groups[-1].append(pivot)
        else:
            groups.append([pivot])

    levels = []
    for group in groups:
        total = 0.0
        starts = []
        for price, bar in group:
            total += price
            starts.append(bar.start)
        starts.sort()
        stamps = []
        for start in starts:
            stamps.append(start.isoformat())
        levels.append(Level(price=total / len(group), touches=len(group), pivot_starts=stamps))
    return levels


def pivot_price(pivot: tuple[float, OHLCV]) -> float:
    return pivot[0]


def level_price(level: Level) -> float:
    return level.price
