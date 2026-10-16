"""Bar storage and time rules shared by the downloader and the replay session.

Every stored bar timestamp is the bar's START in UTC. A bar becomes visible only once it
has fully closed, so nothing revealed to the agent can contain prices from after "now".
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

BAR_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
MARKET_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
# Yahoo serves float32 prices; rounding removes noise like 212.74000549316406.
PRICE_DECIMALS = 4

INTERVALS: dict[str, timedelta] = {
    "5m": timedelta(minutes=5),
    "1d": timedelta(hours=6, minutes=30),  # one regular session, open to close
}


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. `start` and `end` are UTC."""

    symbol: str
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "open": round(self.open, 4),
            "high": round(self.high, 4),
            "low": round(self.low, 4),
            "close": round(self.close, 4),
            "volume": self.volume,
        }


def daily_bar_start(session_day: date) -> datetime:
    """09:30 New York time on `session_day`, in UTC."""
    local = datetime.combine(session_day, MARKET_OPEN, tzinfo=MARKET_TZ)
    return local.astimezone(UTC)


def is_market_open(moment: datetime) -> bool:
    """Regular session on a weekday. Exchange holidays are not modeled (DECISIONS.md D004)."""
    local = moment.astimezone(MARKET_TZ)
    if local.weekday() >= 5:
        return False
    return MARKET_OPEN <= local.time() < MARKET_CLOSE


class BarStore:
    """Read only access to the Parquet fixtures under `root/<interval>/<SYMBOL>.parquet`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[tuple[str, str], list[Bar]] = {}

    def symbols(self, interval: str = "1d") -> list[str]:
        folder = self.root / interval
        names = []
        for path in sorted(folder.glob("*.parquet")):
            names.append(path.stem)
        return names

    def all_bars(self, symbol: str, interval: str) -> list[Bar]:
        """Every stored bar for the symbol, oldest first. Empty list if the file is missing."""
        if interval not in INTERVALS:
            raise ValueError(f"unknown interval {interval!r}; use one of {sorted(INTERVALS)}")
        key = (symbol, interval)
        if key not in self._cache:
            self._cache[key] = self._load(symbol, interval)
        return self._cache[key]

    def _load(self, symbol: str, interval: str) -> list[Bar]:
        path = self.root / interval / f"{symbol}.parquet"
        if not path.is_file():
            return []
        frame = pd.read_parquet(path)
        starts = pd.to_datetime(frame["timestamp"], utc=True).to_list()
        opens = frame["open"].astype(float).round(PRICE_DECIMALS).to_list()
        highs = frame["high"].astype(float).round(PRICE_DECIMALS).to_list()
        lows = frame["low"].astype(float).round(PRICE_DECIMALS).to_list()
        closes = frame["close"].astype(float).round(PRICE_DECIMALS).to_list()
        volumes = frame["volume"].astype(int).to_list()
        length = INTERVALS[interval]
        bars = []
        for i, stamp in enumerate(starts):
            start = stamp.to_pydatetime().astimezone(UTC)
            bars.append(
                Bar(
                    symbol=symbol,
                    start=start,
                    end=start + length,
                    open=opens[i],
                    high=highs[i],
                    low=lows[i],
                    close=closes[i],
                    volume=volumes[i],
                )
            )
        bars.sort(key=bar_start)
        return bars


def bar_start(bar: Bar) -> datetime:
    return bar.start
