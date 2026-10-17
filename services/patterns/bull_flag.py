"""Simplified bull flag: a sharp rise (pole) followed by a shallow, quiet pullback (flag).

The pattern is "forming" when the most recent bars are the flag.
"""

from collections.abc import Sequence

from services.patterns.types import OHLCV, Detection, bar_ref, pct

POLE_MIN_BARS = 3
POLE_MAX_BARS = 7
POLE_MIN_GAIN_PCT = 8.0
FLAG_MIN_BARS = 2
FLAG_MAX_BARS = 6
FLAG_MAX_RETRACE = 0.5

RULE = (
    "bull_flag: pole of 3-7 bars with rising closes, lowest low at its first bar, highest "
    "high at its last bar, gaining >= 8% low to high; then the 2-6 most recent bars form a "
    "flag: every high below the pole high, lowest low retraces <= 50% of the pole, last "
    "flag high <= first flag high, and average flag volume below average pole volume"
)


def bull_flag(bars: Sequence[OHLCV]) -> Detection | None:
    """Detect a bull flag ending at the last bar. None if no pole and flag pair qualifies.

    Tries the shortest flag first. For each flag, picks the pole with the largest gain
    (ties go to the shorter pole).
    """
    for flag_len in range(FLAG_MIN_BARS, FLAG_MAX_BARS + 1):
        if flag_len + POLE_MIN_BARS > len(bars):
            return None
        flag = list(bars[-flag_len:])
        pole = best_pole(bars, end=len(bars) - flag_len)
        if pole is None:
            continue
        if flag_ok(pole, flag):
            return build(pole, flag)
    return None


def best_pole(bars: Sequence[OHLCV], end: int) -> list[OHLCV] | None:
    """The valid pole ending just before index `end` with the largest gain."""
    best: list[OHLCV] | None = None
    best_gain = 0.0
    for pole_len in range(POLE_MIN_BARS, POLE_MAX_BARS + 1):
        if pole_len > end:
            break
        pole = list(bars[end - pole_len : end])
        if not pole_ok(pole):
            continue
        gain = pole_gain_pct(pole)
        if gain > best_gain:
            best = pole
            best_gain = gain
    return best


def pole_ok(pole: list[OHLCV]) -> bool:
    for i in range(1, len(pole)):
        if pole[i].close <= pole[i - 1].close:
            return False
    base = pole[0].low
    top = pole[-1].high
    for bar in pole:
        if bar.low < base or bar.high > top:
            return False
    return pole_gain_pct(pole) >= POLE_MIN_GAIN_PCT


def pole_gain_pct(pole: list[OHLCV]) -> float:
    return (pole[-1].high - pole[0].low) / pole[0].low * 100


def flag_ok(pole: list[OHLCV], flag: list[OHLCV]) -> bool:
    top = pole[-1].high
    floor = top - FLAG_MAX_RETRACE * (top - pole[0].low)
    for bar in flag:
        if bar.high >= top or bar.low < floor:
            return False
    if flag[-1].high > flag[0].high:
        return False
    return average_volume(flag) < average_volume(pole)


def average_volume(bars: list[OHLCV]) -> float:
    total = 0
    for bar in bars:
        total += bar.volume
    return total / len(bars)


def build(pole: list[OHLCV], flag: list[OHLCV]) -> Detection:
    base = pole[0].low
    top = pole[-1].high
    flag_low = min(bar.low for bar in flag)
    refs = []
    for bar in pole:
        refs.append(bar_ref(bar, "pole"))
    for bar in flag:
        refs.append(bar_ref(bar, "flag"))
    return Detection(
        pattern="bull_flag",
        rule=RULE,
        details={
            "pole_bars": len(pole),
            "flag_bars": len(flag),
            "pole_low": base,
            "pole_high": top,
            "pole_gain_pct": pct(top - base, base),
            "flag_low": flag_low,
            "retrace_pct_of_pole": pct(top - flag_low, top - base),
            "flag_volume_vs_pole": round(average_volume(flag) / average_volume(pole), 3),
            "breakout_level": top,
        },
        bars=refs,
    )
