"""Prior day high break: an intraday bar closes above yesterday's high."""

from collections.abc import Sequence
from typing import Any

from services.patterns.types import OHLCV, Detection, bar_ref, pct

RULE = "prior_day_high_break: a closed intraday bar today has close > prior session high"


def prior_day_high_break(prior_day: OHLCV, today_bars: Sequence[OHLCV]) -> Detection | None:
    """Return the first of today's bars that closes above the prior day's high.

    Inputs: the prior session's daily bar and today's closed intraday bars, oldest first.
    A wick above the high without a close above it does not count, and neither does a
    close exactly at the high. Returns None if no bar qualifies.
    """
    level = prior_day.high
    for bar in today_bars:
        if bar.close > level:
            latest = today_bars[-1]
            return Detection(
                pattern="prior_day_high_break",
                rule=RULE,
                details={
                    "prior_day_high": level,
                    "break_close": bar.close,
                    "break_pct": pct(bar.close - level, level),
                    "latest_close": latest.close,
                    "latest_vs_high_pct": pct(latest.close - level, level),
                    "holding_above_high": latest.close > level,
                },
                bars=supporting_bars(prior_day, bar, latest),
            )
    return None


def supporting_bars(prior_day: OHLCV, breakout: OHLCV, latest: OHLCV) -> list[dict[str, Any]]:
    """Prior day, the breakout bar, and the latest bar when it is a different bar, so a
    reader can see whether the break is still holding."""
    refs = [bar_ref(prior_day, "prior_day"), bar_ref(breakout, "breakout_bar")]
    if latest.start != breakout.start:
        refs.append(bar_ref(latest, "latest_bar"))
    return refs
