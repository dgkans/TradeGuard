"""Detectors on hand built bar sequences, with positive, negative and boundary cases."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from services.patterns.atr import atr, true_ranges
from services.patterns.bull_flag import bull_flag
from services.patterns.inside_bar import inside_bar
from services.patterns.levels import support_resistance
from services.patterns.prior_day_high import prior_day_high_break
from services.patterns.types import Candle

T0 = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)


def daily(rows: Sequence[tuple[float, float, float, float]], volume: int = 1000) -> list[Candle]:
    """(open, high, low, close) rows on consecutive days."""
    candles = []
    for i, (o, h, low, c) in enumerate(rows):
        candles.append(Candle(T0 + timedelta(days=i), o, h, low, c, volume))
    return candles


def stamps(detection_bars: list[dict[str, str]]) -> list[str]:
    out = []
    for bar in detection_bars:
        out.append(bar["start"])
    return out


# --- ATR ------------------------------------------------------------------------


def test_true_range_uses_previous_close_on_gaps() -> None:
    bars = daily([(10, 11, 9, 12), (14.5, 15, 14, 14.5)])
    assert true_ranges(bars) == [3.0]  # gap up: high 15 minus prior close 12


def test_atr_wilder_smoothing() -> None:
    bars = daily([(9, 10, 8, 9), (10, 11, 9, 10), (11, 13, 10, 12), (12, 12, 11, 11.5)])
    # TRs: 2, 3, 1. Seed = mean(2, 3) = 2.5, then (2.5 * 1 + 1) / 2 = 1.75
    assert atr(bars, period=2) == pytest.approx(1.75)


def test_atr_needs_period_plus_one_bars() -> None:
    bars = daily([(9, 10, 8, 9), (10, 11, 9, 10)])
    assert atr(bars, period=2) is None


# --- inside bar -------------------------------------------------------------------


def test_inside_bar_fires() -> None:
    bars = daily([(100, 110, 95, 105), (104, 108, 97, 101)])
    detection = inside_bar(bars)
    assert detection is not None
    assert detection.pattern == "inside_bar"
    assert [b["role"] for b in detection.bars] == ["mother_bar", "inside_bar"]
    assert stamps(detection.bars) == [bars[0].start.isoformat(), bars[1].start.isoformat()]
    assert "inside" in detection.rule


@pytest.mark.parametrize(
    "second",
    [
        (104, 110, 97, 101),  # equal high is not strictly inside
        (104, 108, 95, 101),  # equal low
        (104, 111, 97, 101),  # higher high
        (104, 108, 94, 101),  # lower low
    ],
)
def test_inside_bar_negative(second: tuple[float, float, float, float]) -> None:
    assert inside_bar(daily([(100, 110, 95, 105), second])) is None


def test_inside_bar_needs_two_bars() -> None:
    assert inside_bar(daily([(100, 110, 95, 105)])) is None


# --- prior day high break ------------------------------------------------------------


def intraday(closes_and_highs: list[tuple[float, float]]) -> list[Candle]:
    start = T0 + timedelta(days=1)
    bars = []
    for i, (close, high) in enumerate(closes_and_highs):
        bars.append(Candle(start + timedelta(minutes=5 * i), close - 0.2, high, close - 0.5,
                           close, 500))  # fmt: skip
    return bars


def test_prior_day_high_break_fires_on_first_close_above() -> None:
    prior = daily([(95, 100, 94, 98)])[0]
    today = intraday([(99, 99.5), (100, 100.4), (101.2, 101.5), (102, 102.2)])
    detection = prior_day_high_break(prior, today)
    assert detection is not None
    assert detection.pattern == "prior_day_high_break"
    assert detection.details["prior_day_high"] == 100
    assert detection.details["break_close"] == 101.2
    roles = [b["role"] for b in detection.bars]
    assert roles == ["prior_day", "breakout_bar", "latest_bar"]
    assert detection.bars[1]["start"] == today[2].start.isoformat()


def test_close_equal_to_prior_high_is_not_a_break() -> None:
    prior = daily([(95, 100, 94, 98)])[0]
    assert prior_day_high_break(prior, intraday([(99, 99.5), (100, 100)])) is None


def test_wick_above_without_close_is_not_a_break() -> None:
    prior = daily([(95, 100, 94, 98)])[0]
    assert prior_day_high_break(prior, intraday([(99.5, 101), (99.8, 100.9)])) is None


def test_no_intraday_bars_means_no_break() -> None:
    prior = daily([(95, 100, 94, 98)])[0]
    assert prior_day_high_break(prior, []) is None


# --- bull flag ---------------------------------------------------------------------

Row = tuple[float, float, float, float]

BASE: list[Row] = [(100, 101, 99, 100), (100, 101, 99, 100.5), (100.5, 101, 99.5, 100)]
POLE: list[Row] = [(100, 103, 99.5, 102.5), (102.5, 106, 102, 105.5), (105.5, 109, 105, 108.5),
        (108.5, 112, 108, 111.5)]  # fmt: skip
FLAG: list[Row] = [(111, 111.5, 108, 109), (109, 110.8, 107.5, 108.5), (108.5, 110.2, 107, 108)]


def flag_series(
    base: Sequence[Row] = BASE,
    pole: Sequence[Row] = POLE,
    flag: Sequence[Row] = FLAG,
    pole_volume: int = 2000,
    flag_volume: int = 1000,
) -> list[Candle]:
    bars = daily(base, volume=1500)
    for row in pole:
        bars.append(Candle(T0 + timedelta(days=len(bars)), *row, pole_volume))
    for row in flag:
        bars.append(Candle(T0 + timedelta(days=len(bars)), *row, flag_volume))
    return bars


def test_bull_flag_fires() -> None:
    bars = flag_series()
    detection = bull_flag(bars)
    assert detection is not None
    assert detection.pattern == "bull_flag"
    assert detection.details["pole_gain_pct"] == pytest.approx((112 - 99.5) / 99.5 * 100, 0.01)
    assert detection.details["pole_bars"] == 4
    assert detection.details["flag_bars"] == 3
    roles = [b["role"] for b in detection.bars]
    assert roles.count("pole") == 4
    assert roles.count("flag") == 3
    assert detection.bars[-1]["start"] == bars[-1].start.isoformat()


def test_bull_flag_rejects_new_high_inside_flag() -> None:
    flag = [(111, 111.5, 108, 109), (109, 113, 107.5, 108.5), (108.5, 110.2, 107, 108)]
    assert bull_flag(flag_series(flag=flag)) is None


def test_bull_flag_rejects_deep_retrace() -> None:
    # pole base 99.5, top 112: 50% retrace level is 105.75
    flag = [(111, 111.5, 108, 109), (109, 110.8, 105.5, 108.5), (108.5, 110.2, 107, 108)]
    assert bull_flag(flag_series(flag=flag)) is None


def test_bull_flag_rejects_weak_pole() -> None:
    pole = [(100, 101.5, 99.5, 101), (101, 102, 100.5, 101.5), (101.5, 103, 101, 102.5),
            (102.5, 104, 102, 103.5)]  # fmt: skip  # +4.5%
    flag = [(103.5, 103.8, 102.5, 103), (103, 103.6, 102.2, 102.8)]
    assert bull_flag(flag_series(pole=pole, flag=flag)) is None


def test_bull_flag_rejects_heavy_flag_volume() -> None:
    assert bull_flag(flag_series(pole_volume=1000, flag_volume=2000)) is None


def test_bull_flag_rejects_rising_flag_highs() -> None:
    flag = [(111, 110.2, 108, 109), (109, 110.8, 107.5, 108.5), (108.5, 111.5, 107, 108)]
    assert bull_flag(flag_series(flag=flag)) is None


def test_bull_flag_needs_a_flag() -> None:
    # Pole just ended: no consolidation yet.
    assert bull_flag(flag_series(flag=[])) is None


# --- support and resistance ------------------------------------------------------------


def test_levels_cluster_pivots_and_split_around_price() -> None:
    # Two swing highs near 120 and two swing lows near 100, ending at 110.
    closes = [105, 110, 115, 119, 115, 110, 104, 101, 104, 110, 115, 119.5, 115, 110, 104,
              100.5, 104, 108, 110]  # fmt: skip
    rows: list[tuple[float, float, float, float]] = []
    for close in closes:
        rows.append((close, close + 0.5, close - 0.5, close))
    bars = daily(rows)
    result = support_resistance(bars, atr_value=2.0)

    assert len(result.resistance) == 1
    assert result.resistance[0].touches == 2
    assert result.resistance[0].price == pytest.approx((119.5 + 120) / 2)
    assert len(result.support) == 1
    assert result.support[0].touches == 2
    assert result.support[0].price == pytest.approx((100.5 + 100) / 2)
    pivot_times = result.resistance[0].pivot_starts
    assert pivot_times == [bars[3].start.isoformat(), bars[11].start.isoformat()]


def test_levels_ignore_unconfirmed_pivot_at_the_end() -> None:
    # The last bar is the highest, but there are no bars after it to confirm a pivot.
    rows: list[tuple[float, float, float, float]] = []
    for close in [100, 101, 102, 103, 104, 105]:
        rows.append((close, close + 0.5, close - 0.5, close))
    result = support_resistance(daily(rows), atr_value=1.0)
    assert result.resistance == []
    assert result.support == []


def test_prior_day_high_break_flags_failed_break() -> None:
    """Regression: a break that has since fallen back below the high must say so."""
    prior = daily([(95, 100, 94, 98)])[0]
    holding = prior_day_high_break(prior, intraday([(101, 101.5), (102, 102.2)]))
    failed = prior_day_high_break(prior, intraday([(101, 101.5), (99.5, 101)]))
    assert holding is not None and holding.details["holding_above_high"] is True
    assert failed is not None and failed.details["holding_above_high"] is False
    assert failed.bars[-1]["role"] == "latest_bar"
