"""Average True Range (Wilder)."""

from collections.abc import Sequence

from services.patterns.types import OHLCV

DEFAULT_PERIOD = 14


def true_ranges(bars: Sequence[OHLCV]) -> list[float]:
    """True range of every bar after the first: the largest of high minus low, and the
    distances from the previous close to this bar's high and low (which captures gaps)."""
    ranges = []
    for i in range(1, len(bars)):
        bar = bars[i]
        prev_close = bars[i - 1].close
        ranges.append(
            max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        )
    return ranges


def atr(bars: Sequence[OHLCV], period: int = DEFAULT_PERIOD) -> float | None:
    """Wilder ATR as of the last bar.

    Seeded with the simple mean of the first `period` true ranges, then smoothed as
    ATR = (previous ATR * (period - 1) + TR) / period.
    Returns None when there are fewer than period + 1 bars.
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    ranges = true_ranges(bars)
    if len(ranges) < period:
        return None
    value = sum(ranges[:period]) / period
    for tr in ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value
