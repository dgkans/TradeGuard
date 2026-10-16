"""Download historical bars into fixtures/bars/ as Parquet.

Run once and commit the output, so replays never depend on the network:

    uv run python -m services.replay_server.download

Daily bars cover about two years. Yahoo only serves 5 minute bars for the last 60 days,
so intraday scenarios must start inside that window (see DECISIONS.md D004).
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

from services.replay_server.bars import BAR_COLUMNS, daily_bar_start

SYMBOLS = [
    # In the default policy allowlist
    "NVDA", "AMD", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "SPY", "QQQ",
    # Outside it, so policy tests have real data for unknown symbols
    "NFLX", "AVGO", "JPM", "XOM", "COIN", "PLTR", "INTC", "BA", "DIS", "IWM",
]  # fmt: skip

DEFAULT_OUT = Path("fixtures/bars")


def fetch(symbol: str, interval: str, period: str) -> pd.DataFrame:
    """Fetch one symbol and normalize to BAR_COLUMNS with UTC bar start timestamps.

    Raises RuntimeError when Yahoo returns no rows.
    """
    raw = yf.Ticker(symbol).history(
        period=period, interval=interval, auto_adjust=False, prepost=False
    )
    if raw.empty:
        raise RuntimeError(f"no {interval} data returned for {symbol}")

    frame = pd.DataFrame(
        {
            "open": raw["Open"].astype("float64"),
            "high": raw["High"].astype("float64"),
            "low": raw["Low"].astype("float64"),
            "close": raw["Close"].astype("float64"),
            "volume": raw["Volume"].astype("int64"),
        }
    )
    if interval == "1d":
        # Yahoo stamps daily bars at local midnight. Store the session open instead,
        # so every bar's timestamp means "bar start" regardless of interval.
        starts = []
        for stamp in raw.index:
            starts.append(daily_bar_start(stamp.date()))
        frame.insert(0, "timestamp", pd.DatetimeIndex(starts))
    else:
        frame.insert(0, "timestamp", raw.index.tz_convert("UTC"))

    frame = frame.dropna().reset_index(drop=True)
    return frame[BAR_COLUMNS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--symbols", nargs="*", default=SYMBOLS)
    args = parser.parse_args(argv)

    jobs = [("1d", "2y"), ("5m", "60d")]
    failures: list[str] = []
    for interval, period in jobs:
        folder = args.out / interval
        folder.mkdir(parents=True, exist_ok=True)
        for symbol in args.symbols:
            try:
                frame = fetch(symbol, interval, period)
            except Exception as exc:  # network and parsing errors are reported, not fatal
                failures.append(f"{symbol} {interval}: {exc}")
                continue
            frame.to_parquet(folder / f"{symbol}.parquet", index=False)
            first = frame["timestamp"].iloc[0]
            last = frame["timestamp"].iloc[-1]
            print(f"{symbol:5} {interval:3} {len(frame):6} bars  {first} .. {last}")

    for failure in failures:
        print(f"FAILED {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
