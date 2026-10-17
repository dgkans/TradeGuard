"""Approval store: lifecycle, expiry, exactly once claim, audit rows. Memory and Postgres."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest

from services.common.config import load_settings
from services.gateway.approvals import (
    ApprovalConflictError,
    ApprovalNotFoundError,
    ApprovalStore,
    MemoryApprovalStore,
    PostgresApprovalStore,
)
from services.gateway.audit import MemoryAuditSink, migrate

ARGS = {"symbol": "NVDA", "side": "buy", "notional": 1500}


def database_url() -> str:
    return load_settings().database_url.get_secret_value()


def postgres_available() -> bool:
    try:
        with psycopg.connect(database_url(), connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ApprovalStore]:
    if request.param == "memory":
        yield MemoryApprovalStore(audit=MemoryAuditSink())
        return
    if not postgres_available():
        pytest.skip("Postgres not reachable")
    migrate(database_url())
    async with PostgresApprovalStore(database_url()) as pg:
        yield pg


async def create(store: ApprovalStore, timeout_s: float = 60, **kw: Any) -> Any:
    return await store.create(
        session_id=kw.get("session_id", f"s-{uuid.uuid4().hex[:6]}"),
        tool="place_order",
        args=ARGS,
        rule_id="approval_notional",
        reason="order size $1500.00 is over the approval threshold $1000.00",
        audit_event_id=None,
        timeout_s=timeout_s,
    )


async def test_new_approval_is_pending(store: ApprovalStore) -> None:
    approval = await create(store)
    assert approval.id.startswith("apr_")
    assert approval.status == "pending"
    assert approval.args == ARGS
    fetched = await store.get(approval.id)
    assert fetched is not None and fetched.status == "pending"
    pending = await store.list(status="pending", session_id=approval.session_id)
    assert [a.id for a in pending] == [approval.id]


async def test_approve_then_claim_exactly_once(store: ApprovalStore) -> None:
    approval = await create(store)
    decided = await store.decide(approval.id, approve=True, decided_by="alice", note="ok")
    assert (decided.status, decided.decided_by, decided.note) == ("approved", "alice", "ok")
    assert decided.decided_at is not None
    first, second = await asyncio.gather(store.claim(approval.id), store.claim(approval.id))
    claimed = [c for c in (first, second) if c is not None]
    assert len(claimed) == 1
    assert claimed[0].status == "executing"
    done = await store.finish(approval.id, "executed", {"status": "new"})
    assert done.status == "executed"
    assert done.result == {"status": "new"}
    assert await store.claim(approval.id) is None


async def test_reject_records_note(store: ApprovalStore) -> None:
    approval = await create(store)
    rejected = await store.decide(approval.id, approve=False, decided_by="bob", note="too big")
    assert (rejected.status, rejected.note) == ("rejected", "too big")
    assert await store.claim(approval.id) is None


async def test_cannot_decide_twice(store: ApprovalStore) -> None:
    approval = await create(store)
    await store.decide(approval.id, approve=False, decided_by="bob", note="")
    with pytest.raises(ApprovalConflictError, match="rejected"):
        await store.decide(approval.id, approve=True, decided_by="alice", note="")


async def test_timeout_auto_rejects(store: ApprovalStore) -> None:
    approval = await create(store, timeout_s=0.2)
    await asyncio.sleep(0.35)
    expired = await store.get(approval.id)
    assert expired is not None
    assert expired.status == "expired"
    with pytest.raises(ApprovalConflictError, match="expired"):
        await store.decide(approval.id, approve=True, decided_by="alice", note="late")
    assert await store.claim(approval.id) is None


async def test_unknown_approval(store: ApprovalStore) -> None:
    assert await store.get("apr_missing") is None
    with pytest.raises(ApprovalNotFoundError):
        await store.decide("apr_missing", approve=True, decided_by="x", note="")


async def test_memory_store_audits_every_decision() -> None:
    audit = MemoryAuditSink()
    store = MemoryApprovalStore(audit=audit)
    approved = await create(store, session_id="aud")
    rejected = await create(store, session_id="aud")
    expired = await create(store, session_id="aud", timeout_s=0.05)
    await store.decide(approved.id, approve=True, decided_by="alice", note="fine")
    await store.decide(rejected.id, approve=False, decided_by="bob", note="no")
    await asyncio.sleep(0.1)
    await store.get(expired.id)
    rows = [(e["decision"], e["rule_id"]) for e in audit.events]
    assert rows == [
        ("ALLOW", "human_approval"),
        ("DENY", "human_rejection"),
        ("DENY", "approval_timeout"),
    ]
    assert "alice" in audit.events[0]["reason"] and "fine" in audit.events[0]["reason"]
    assert all(e["session_id"] == "aud" for e in audit.events)


@pytest.mark.skipif(not postgres_available(), reason="Postgres not reachable")
async def test_postgres_store_audits_decisions_in_audit_events() -> None:
    migrate(database_url())
    session_id = f"pg-apr-{uuid.uuid4().hex[:6]}"
    async with PostgresApprovalStore(database_url()) as store:
        approval = await create(store, session_id=session_id)
        await store.decide(approval.id, approve=False, decided_by="bob", note="not today")
    with psycopg.connect(database_url()) as conn:
        rows = conn.execute(
            "select decision, rule_id, reason from audit_events where session_id = %s",
            (session_id,),
        ).fetchall()
    assert rows == [("DENY", "human_rejection", f"{approval.id} rejected by bob: not today")]
