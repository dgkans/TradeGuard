"""Shared types for the price action detectors.

Detectors accept any object with OHLCV attributes (the OHLCV protocol), so they work on
replay server bars, Alpaca bars or the `Candle` test helper alike.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class OHLCV(Protocol):
    @property
    def start(self) -> datetime: ...
    @property
    def open(self) -> float: ...
    @property
    def high(self) -> float: ...
    @property
    def low(self) -> float: ...
    @property
    def close(self) -> float: ...
    @property
    def volume(self) -> int: ...


@dataclass(frozen=True)
class Candle:
    """Minimal OHLCV bar. `start` is the bar's start time."""

    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Detection:
    """One pattern that fired.

    `rule` is the plain English rule, `details` the numbers that satisfied it, and `bars`
    the supporting bars in time order, each tagged with its role in the pattern.
    """

    pattern: str
    rule: str
    details: dict[str, Any]
    bars: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "rule": self.rule,
            "details": dict(self.details),
            "bars": list(self.bars),
        }


def bar_ref(bar: OHLCV, role: str) -> dict[str, Any]:
    """Compact reference to a supporting bar."""
    return {
        "role": role,
        "start": bar.start.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def pct(numerator: float, denominator: float) -> float:
    """Percentage rounded to 2 decimals."""
    return round(numerator / denominator * 100, 2)
