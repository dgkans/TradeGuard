"""Market overview (F10d): drawn from the replay fixtures, nothing made up."""

from fastapi.testclient import TestClient

from services.api.app import ApiDeps, create_app
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import MemoryAuditReader, MemoryAuditSink


def test_overview_has_indices_and_a_watchlist_with_sparklines() -> None:
    audit = MemoryAuditSink()
    deps = ApiDeps(approvals=MemoryApprovalStore(audit=audit), audit=MemoryAuditReader(audit))
    with TestClient(create_app(deps)) as client:
        data = client.get("/market/overview").json()
    assert data["as_of"].startswith("2026-09-15T14:30")
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
