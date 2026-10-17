# Price action detectors

Pure functions over OHLCV bars, oldest first. They accept any object with `start, open,
high, low, close, volume` (the `OHLCV` protocol in `types.py`). The caller passes only bars
that have closed, so the detectors can't see the future. In the replay server, the tools
`detect_patterns` and `scan_watchlist` enforce this using the sim clock.

Every fired pattern returns:
- `pattern`: rule id
- `rule`: the rule in plain English
- `details`: the numbers that satisfied it
- `bars`: the supporting bars, each with a `role` and its `start` timestamp

These rules are deliberately simple and fixed. They are building blocks for an agent
demo, not a trading strategy and not financial advice.

## Prior day high break (`prior_day_high_break`)
Timeframe: today's closed 5 minute bars against the prior session's daily bar.

Fires on the **first** intraday bar today whose **close** is strictly above the prior
session's high.
- A wick above the high without a close above it does not count.
- A close exactly at the high does not count.
- `holding_above_high`: whether the latest closed bar is still above the high. `false`
  means the break has failed.
- `latest_vs_high_atr`: distance of the latest close above the high, in daily ATR(14).
  Used to compare breakout strength across symbols.
- Bars: `prior_day`, `breakout_bar`, and `latest_bar` when it is a different bar.
- Not evaluated (listed under `not_evaluated` with a reason) when there is no prior day
  bar or no closed intraday bar today yet, for example before the open.

## Inside bar (`inside_bar`)
Timeframe: daily.

Fires when the last closed daily bar has `high < previous high` **and** `low > previous
low`. Equal highs or lows don't count. `range_ratio` is the inside bar's range divided by
the mother bar's range. Bars: `mother_bar`, `inside_bar`.

## Bull flag (`bull_flag`, simplified)
Timeframe: daily. "Forming" means the most recent bars are the flag.

**Pole**, 3 to 7 bars ending just before the flag:
- every close is higher than the previous close
- the lowest low is the first bar's low, and the highest high is the last bar's high
- gain from that low to that high is at least 8%

**Flag**, the 2 to 6 most recent bars:
- every high is below the pole high (no new high yet)
- the lowest low retraces at most 50% of the pole's height
- the last flag high is at or below the first flag high (flat or downward drift)
- average flag volume is below average pole volume

Search order: the shortest flag first. For each flag length, the valid pole with the
largest gain is chosen (ties go to the shorter pole).

`breakout_level` is the pole high, the level a continuation would need to clear. Bars:
`pole` × n, then `flag` × m.

## ATR (`atr_14`)
Wilder's Average True Range over 14 daily bars.
- True range = max(high − low, |high − previous close|, |low − previous close|).
- The seed is the mean of the first 14 true ranges. After that,
  `ATR = (ATR_prev × 13 + TR) / 14`.
- `null` with fewer than 15 bars.

## Support and resistance (`levels`)
Timeframe: daily, last 120 closed bars.
1. **Swing high**: a bar whose high is above the highs of the 2 bars on each side.
   **Swing low**: a bar whose low is below the lows of the 2 bars on each side. The newest
   2 bars can't be pivots because there's nothing after them to confirm one.
2. Sort the pivot prices and merge any pivot within 0.5 × ATR of its group's lowest
   price (1% of the last close if there is no ATR). A level's price is the mean of its
   group, and `touches` is the group size.
3. Levels above the last close are **resistance**, the rest are **support**. Up to 3 of
   each are returned, nearest first, with the timestamps of their pivots.

## Code
| File | Function |
|---|---|
| `prior_day_high.py` | `prior_day_high_break(prior_day, today_bars)` |
| `inside_bar.py` | `inside_bar(bars)` |
| `bull_flag.py` | `bull_flag(bars)` |
| `atr.py` | `atr(bars, period=14)`, `true_ranges(bars)` |
| `levels.py` | `support_resistance(bars, atr_value)` |
| `analyze.py` | `analyze(...)` runs everything for one symbol; `summarize(...)` is the compact scan form |

Thresholds are module-level constants at the top of each file. Tests on hand-built bar
sequences are in `tests/patterns/`.
