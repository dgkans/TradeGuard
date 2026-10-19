"""Market overview (F10d, F10f): from the live feed, or the replay fixtures; nothing made up."""

from fastapi.testclient import TestClient

from services.api.app import ApiDeps, create_app
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import MemoryAuditReader, MemoryAuditSink


def test_overview_has_indices_and_a_watchlist_with_sparklines() -> None:
    audit = MemoryAuditSink()
    deps = ApiDeps(approvals=MemoryApprovalStore(audit=audit), audit=MemoryAuditReader(audit))
    with TestClient(create_app(deps)) as client:
        data = client.get("/market/overview").json()
    assert data["as_of"].startswith("2026-10-01T14:30")  # the desk's start (no clock here)
    assert data["market_open"] is True
    assert [s["symbol"] for s in data["indices"]] == ["SPY", "QQQ"]
    symbols = [s["symbol"] for s in data["watchlist"]]
    assert symbols[:5] == ["NVDA", "AMD", "AAPL", "MSFT", "TSLA"]
    for item in data["indices"] + data["watchlist"]:
        assert item["price"] > 0
        assert isinstance(item["day_change_pct"], float)
        assert len(item["spark"]) >= 20  # 5 minute closes, prior session and today so far
        assert item["spark"][-1] == item["price"]
    movers = data["movers"]
    assert movers == sorted(movers, key=lambda m: -abs(m["day_change_pct"]))[: len(movers)]


async def test_overview_uses_live_prices_when_there_is_a_feed() -> None:
    from pathlib import Path

    from services.api.live import LiveFeed
    from services.api.market import overview
    from tests.api.fake_yahoo import OPEN_MOMENT, Clock, FakeYahoo

    root = Path(__file__).resolve().parents[2]
    clock = Clock(OPEN_MOMENT)
    yahoo = FakeYahoo(root, clock)
    feed = LiveFeed(["SPY", "QQQ", "NVDA", "AMD", "AAPL", "MSFT", "TSLA", "AMZN", "META",
                     "GOOGL"], yahoo, clock=clock)  # fmt: skip
    await feed.refresh()
    yahoo.prices["SPY"] = 700.0
    await feed.refresh()
    data = overview(root, feed)
    assert data["live"] is True and data["source"] == "Yahoo Finance"
    assert data["as_of"] == OPEN_MOMENT.isoformat() and data["market_open"] is True
    spy = data["indices"][0]
    assert spy["symbol"] == "SPY" and spy["price"] == 700.0 and spy["spark"][-1] == 700.0
    assert spy["previous_close"] == feed.previous_close("SPY")
    assert len(data["watchlist"]) == 8
