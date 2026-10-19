"""Market overview for the landing page and the chat side panel (F10d).

Everything comes from the replay fixtures at the desk scenario's start time: prices,
day change against the prior close, and 5 minute closes for sparklines. Nothing is made
up, and nothing after the market's "now" is shown.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from services.replay_server.scenario import load_scenario
from services.replay_server.session import ReplaySession

INDICES = ["SPY", "QQQ"]
SPARK_BARS = 96  # about the prior session (78 bars) and today so far


def summary(market: ReplaySession, symbol: str) -> dict[str, Any] | None:
    try:
        price = float(market.get_quote(symbol)["price"])
        daily = market.get_bars(symbol, "1d", 1)["bars"]
        intraday = market.get_bars(symbol, "5m", SPARK_BARS)["bars"]
    except Exception:  # a symbol without data is left out, not invented
        return None
    previous = float(daily[-1]["close"]) if daily else None
    spark = []
    for bar in intraday:
        spark.append(round(float(bar["close"]), 2))
    if not spark or spark[-1] != round(price, 2):
        spark.append(round(price, 2))
    return {
        "symbol": symbol,
        "price": round(price, 2),
        "previous_close": previous,
        "day_change_pct": round((price - previous) / previous * 100, 2) if previous else 0.0,
        "spark": spark,
    }


@lru_cache(maxsize=2)
def overview(root: Path, scenario_id: str = "desk") -> dict[str, Any]:
    """Indices, the scenario's watchlist plus a few more allowed stocks, and top movers."""
    scenario = load_scenario(scenario_id, root / "scenarios")
    market = ReplaySession(scenario, bars_root=root / "fixtures/bars",
                           news_root=root / "fixtures/news")  # fmt: skip
    extra = ["AMZN", "META", "GOOGL"]
    symbols = []
    for symbol in [*scenario.watchlist, *extra]:
        if symbol not in symbols and symbol not in INDICES:
            symbols.append(symbol)
    indices = [item for item in (summary(market, s) for s in INDICES) if item]
    watchlist = [item for item in (summary(market, s) for s in symbols) if item]
    movers = sorted(watchlist, key=lambda item: -abs(item["day_change_pct"]))[:6]
    clock = market.get_clock()
    return {
        "as_of": clock["now"],
        "market_open": clock["market_open"],
        "indices": indices,
        "watchlist": watchlist,
        "movers": [{"symbol": m["symbol"], "day_change_pct": m["day_change_pct"]} for m in movers],
    }
