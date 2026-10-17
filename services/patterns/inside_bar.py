"""Inside bar: the last bar's range sits strictly inside the bar before it."""

from collections.abc import Sequence

from services.patterns.types import OHLCV, Detection, bar_ref

RULE = "inside_bar: last bar high < previous high AND last bar low > previous low"


def inside_bar(bars: Sequence[OHLCV]) -> Detection | None:
    """Detect an inside bar on the last two bars. None if it doesn't fire or < 2 bars.

    Equal highs or lows don't count: the range must be strictly contained.
    """
    if len(bars) < 2:
        return None
    mother = bars[-2]
    inside = bars[-1]
    if not (inside.high < mother.high and inside.low > mother.low):
        return None
    mother_range = mother.high - mother.low
    return Detection(
        pattern="inside_bar",
        rule=RULE,
        details={
            "mother_high": mother.high,
            "mother_low": mother.low,
            "inside_high": inside.high,
            "inside_low": inside.low,
            "range_ratio": round((inside.high - inside.low) / mother_range, 3),
        },
        bars=[bar_ref(mother, "mother_bar"), bar_ref(inside, "inside_bar")],
    )
