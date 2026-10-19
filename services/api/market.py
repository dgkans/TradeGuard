"""Market overview for the landing page and the chat side panel (F10d, F10f).

With the live feed (F10f) everything comes from Yahoo Finance's latest prices and bars;
without it (tests, no network) from the replay fixtures at the desk scenario's start time.
Prices, day change against the prior close, and 5 minute closes for sparklines. Nothing
is made up.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from services.api.live import LiveFeed, LiveMarket
from services.replay_server.scenario import load_scenario
from services.replay_server.session import ReplaySession

INDICES = ["SPY", "QQQ"]
EXTRA = ["AMZN", "META", "GOOGL"]
SPARK_BARS = 96  # about the prior session (78 bars) and today so far


def summary(market: ReplaySession, symbol: str) -> dict[str, Any] | None:
    try:
        price = float(market.get_quote(symbol)["price"])
        intraday = market.get_bars(symbol, "5m", SPARK_BARS)["bars"]
        if isinstance(market, LiveMarket):
            previous = market.previous_close(symbol)
        else:
            daily = market.get_bars(symbol, "1d", 1)["bars"]
            previous = float(daily[-1]["close"]) if daily else None
    except Exception:  # a symbol without data is left out, not invented
        return None
    spark = []
    for bar in intraday:
        spark.append(round(float(bar["close"]), 2))
    if len(spark) < 2 and previous:
        spark.insert(0, round(previous, 2))  # little history yet: from the previous close
    if not spark or spark[-1] != round(price, 2):
        spark.append(round(price, 2))
    return {
        "symbol": symbol,
        "price": round(price, 2),
        "previous_close": previous,
        "day_change_pct": round((price - previous) / previous * 100, 2) if previous else 0.0,
        "spark": spark,
    }


def _build(market: ReplaySession, watchlist: list[str]) -> dict[str, Any]:
    symbols = []
    for symbol in [*watchlist, *EXTRA]:
        if symbol not in symbols and symbol not in INDICES:
            symbols.append(symbol)
    indices = [item for item in (summary(market, s) for s in INDICES) if item]
    listed = [item for item in (summary(market, s) for s in symbols) if item]
    movers = sorted(listed, key=lambda item: -abs(item["day_change_pct"]))[:6]
    clock = market.get_clock()
    return {
        "as_of": clock["now"],
        "market_open": clock["market_open"],
        "indices": indices,
        "watchlist": listed,
        "movers": [{"symbol": m["symbol"], "day_change_pct": m["day_change_pct"]} for m in movers],
    }


def overview(root: Path, feed: LiveFeed | None = None, scenario_id: str = "desk") -> dict[str, Any]:
    """Indices, the scenario's watchlist plus a few more stocks, and top movers."""
    if feed is None:
        return _replay_overview(root, scenario_id)
    scenario = load_scenario(scenario_id, root / "scenarios")
    data = _build(LiveMarket(scenario, feed, cash=0), scenario.watchlist)
    times = [item.at for item in (feed.quote(s) for s in feed.symbols) if item is not None]
    return {**data, "live": True, "source": feed.source,
            "prices_as_of": max(times).isoformat() if times else None}  # fmt: skip


@lru_cache(maxsize=2)
def _replay_overview(root: Path, scenario_id: str) -> dict[str, Any]:
    scenario = load_scenario(scenario_id, root / "scenarios")
    market = ReplaySession(scenario, bars_root=root / "fixtures/bars",
                           news_root=root / "fixtures/news")  # fmt: skip
    return {**_build(market, scenario.watchlist), "live": False, "source": "replay",
            "prices_as_of": None}  # fmt: skip
