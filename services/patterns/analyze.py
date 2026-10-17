"""Run every detector for one symbol and assemble an explainable result."""

from collections.abc import Sequence
from typing import Any

from services.patterns.atr import DEFAULT_PERIOD, atr
from services.patterns.bull_flag import bull_flag
from services.patterns.inside_bar import inside_bar
from services.patterns.levels import support_resistance
from services.patterns.prior_day_high import prior_day_high_break
from services.patterns.types import OHLCV, Detection

DAILY_PATTERNS = ["inside_bar", "bull_flag"]


def analyze(
    symbol: str,
    daily: Sequence[OHLCV],
    prior_day: OHLCV | None,
    today_intraday: Sequence[OHLCV],
) -> dict[str, Any]:
    """Detect patterns for one symbol.

    Inputs: closed daily bars (oldest first), the prior session's daily bar (or None), and
    today's closed intraday bars. The caller guarantees none of them are from the future.
    Output: fired detections with their rules and bars, the names of patterns checked
    that did not fire, patterns that could not be checked and why, ATR and levels.
    """
    fired: list[Detection] = []
    not_fired: list[str] = []
    not_evaluated: list[dict[str, str]] = []
    atr_value = atr(daily, DEFAULT_PERIOD)

    daily_checks = [("inside_bar", inside_bar(daily)), ("bull_flag", bull_flag(daily))]
    for name, detection in daily_checks:
        if detection is None:
            not_fired.append(name)
        else:
            fired.append(detection)

    if prior_day is None:
        not_evaluated.append({"pattern": "prior_day_high_break", "reason": "no prior day bar"})
    elif not today_intraday:
        not_evaluated.append(
            {"pattern": "prior_day_high_break", "reason": "no closed intraday bars today yet"}
        )
    else:
        breakout = prior_day_high_break(prior_day, today_intraday)
        if breakout is None:
            not_fired.append("prior_day_high_break")
        else:
            fired.append(with_atr_strength(breakout, atr_value))

    rows = []
    for detection in fired:
        rows.append(detection.to_dict())
    return {
        "symbol": symbol,
        "last_close": daily[-1].close if daily else None,
        "atr_14": round(atr_value, 4) if atr_value is not None else None,
        "patterns": rows,
        "not_fired": not_fired,
        "not_evaluated": not_evaluated,
        "levels": support_resistance(daily, atr_value).to_dict(),
    }


def with_atr_strength(detection: Detection, atr_value: float | None) -> Detection:
    """Add how far the latest close is above the broken high, in daily ATRs."""
    if atr_value is None or atr_value <= 0:
        return detection
    details = dict(detection.details)
    distance = details["latest_close"] - details["prior_day_high"]
    details["latest_vs_high_atr"] = round(distance / atr_value, 3)
    return Detection(detection.pattern, detection.rule, details, detection.bars)


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    """Compact form for watchlist scans: each detection keeps its rule, details and the
    timestamps of its supporting bars, but not their prices."""
    patterns = []
    for detection in result["patterns"]:
        bars = []
        for bar in detection["bars"]:
            bars.append({"role": bar["role"], "start": bar["start"]})
        patterns.append(
            {
                "pattern": detection["pattern"],
                "rule": detection["rule"],
                "details": detection["details"],
                "bars": bars,
            }
        )
    return {
        "symbol": result["symbol"],
        "last_close": result["last_close"],
        "atr_14": result["atr_14"],
        "patterns": patterns,
        "not_fired": result["not_fired"],
        "not_evaluated": result["not_evaluated"],
    }
