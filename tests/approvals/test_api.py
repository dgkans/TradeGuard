"""Approval HTTP API."""

import time

from fastapi.testclient import TestClient

from services.gateway.api.app import create_app
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import MemoryAuditSink


async def seeded(timeout_s: float = 60) -> tuple[MemoryApprovalStore, str]:
    store = MemoryApprovalStore(audit=MemoryAuditSink())
    approval = await store.create(
        session_id="api", tool="place_order",
        args={"symbol": "NVDA", "side": "buy", "notional": 1500},
        rule_id="approval_notional", reason="over threshold", audit_event_id=None,
        timeout_s=timeout_s,
    )  # fmt: skip
    return store, approval.id


async def test_list_and_get_pending() -> None:
    store, approval_id = await seeded()
    with TestClient(create_app(store)) as client:
        listed = client.get("/approvals", params={"status": "pending"}).json()
        assert [a["id"] for a in listed["approvals"]] == [approval_id]
        one = client.get(f"/approvals/{approval_id}").json()
        assert one["status"] == "pending"
        assert one["args"]["notional"] == 1500
        assert one["reason"] == "over threshold"
        assert client.get("/approvals/apr_nope").status_code == 404
        assert client.get("/healthz").json() == {"status": "ok"}


async def test_approve_and_conflict() -> None:
    store, approval_id = await seeded()
    with TestClient(create_app(store)) as client:
        ok = client.post(f"/approvals/{approval_id}/approve",
                         json={"decided_by": "alice", "note": "fine"})  # fmt: skip
        assert ok.status_code == 200
        assert (ok.json()["status"], ok.json()["decided_by"]) == ("approved", "alice")
        again = client.post(f"/approvals/{approval_id}/reject", json={"decided_by": "bob"})
        assert again.status_code == 409
        assert "approved" in again.json()["detail"]


async def test_reject_with_note() -> None:
    store, approval_id = await seeded()
    with TestClient(create_app(store)) as client:
        response = client.post(f"/approvals/{approval_id}/reject",
                               json={"decided_by": "bob", "note": "too big"})  # fmt: skip
    assert response.json()["status"] == "rejected"
    assert response.json()["note"] == "too big"


async def test_expired_cannot_be_approved() -> None:
    store, approval_id = await seeded(timeout_s=0.05)
    time.sleep(0.1)
    with TestClient(create_app(store)) as client:
        assert client.get(f"/approvals/{approval_id}").json()["status"] == "expired"
        late = client.post(f"/approvals/{approval_id}/approve", json={"decided_by": "alice"})
    assert late.status_code == 409


async def test_decided_by_is_required_and_bounded() -> None:
    store, approval_id = await seeded()
    with TestClient(create_app(store)) as client:
        assert client.post(f"/approvals/{approval_id}/approve", json={}).status_code == 422
        long_note = {"decided_by": "a", "note": "x" * 2001}
        assert client.post(f"/approvals/{approval_id}/approve", json=long_note).status_code == 422
