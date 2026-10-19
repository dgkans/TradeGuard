"""The shared desk clock (F10e): one replayed market time for every trader and the landing.

The demo market replays real historical bars. The clock moves 5 market minutes per tick
during a session; at the close it stops on the closing time, and the next tick jumps to
the next session's open (nights and weekends are skipped). At the end of the data it
stops for good. Times are what the bar data says, never "today".
"""

from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from services.replay_server.bars import BarStore, bar_start

STEP = timedelta(minutes=5)
CLOCK_SYMBOL = "SPY"  # the session calendar comes from the index ETF's 5 minute bars


@lru_cache(maxsize=4)
def trading_sessions(bars_root: Path) -> dict[datetime, datetime]:
    """{open: close} for every session in the 5 minute data (close = last bar start + 5)."""
    days: dict[object, list[datetime]] = {}
    for bar in BarStore(bars_root).all_bars(CLOCK_SYMBOL, "5m"):
        start = bar_start(bar)
        days.setdefault(start.date(), []).append(start)
    sessions = {}
    for starts in days.values():
        sessions[min(starts)] = max(starts) + STEP
    return dict(sorted(sessions.items()))


class DeskClock:
    """Market time shared by all traders. `tick()` moves it on; None means the data ended."""

    def __init__(self, bars_root: Path, start: datetime) -> None:
        self.sessions = trading_sessions(bars_root)
        self.now = start
        self.ended = False

    def _session_of(self, moment: datetime) -> tuple[datetime, datetime] | None:
        for open_, close in self.sessions.items():
            if open_ <= moment < close:
                return open_, close
        return None

    def _next_open(self, moment: datetime) -> datetime | None:
        for open_ in self.sessions:
            if open_ > moment:
                return open_
        return None

    def tick(self) -> datetime | None:
        """The next market time: +5 minutes in a session, the close, or the next open."""
        if self.ended:
            return None
        session = self._session_of(self.now)
        if session is not None:
            self.now = min(self.now + STEP, session[1])
            return self.now
        following = self._next_open(self.now)
        if following is None:
            self.ended = True
            return None
        self.now = following
        return self.now

    def minutes_since(self, earlier: datetime) -> int:
        return int((self.now - earlier).total_seconds() // 60)
