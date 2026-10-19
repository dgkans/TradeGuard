"""Postgres audit sink against a real database (skipped when Postgres is unreachable)."""

import uuid

import psycopg
import pytest

from services.common.config import load_settings
from services.gateway.audit import AuditOutcome, DecisionRecord, PostgresAuditSink, migrate


def database_url() -> str:
    return load_settings().database_url.get_secret_value()


def postgres_available() -> bool:
    try:
        with psycopg.connect(database_url(), connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not postgres_available(), reason="Postgres not reachable")


async def test_decision_and_outcome_round_trip() -> None:
    migrate(database_url())
    session_id = f"pg-{uuid.uuid4().hex[:8]}"
    async with PostgresAuditSink(database_url()) as sink:
        event_id = await sink.record_decision(
            DecisionRecord(
                session_id=session_id,
                upstream="replay",
                tool="place_order",
                args={"symbol": "AMD", "side": "buy", "notional": 500},
                decision="ALLOW",
                rule_id="pass_through",
                reason="F4 pass through",
                trace_id="0af7651916cd43dd8448eb211c80319c",
                span_id="b7ad6b7169203331",
            )
        )
        await sink.record_outcome(
            event_id,
            AuditOutcome(latency_ms=3.5, upstream_ms=2.0, is_error=False,
                         result_summary='{"status": "new"}'),
        )  # fmt: skip

    with psycopg.connect(database_url()) as conn:
        row = conn.execute(
            "select session_id, tool, args, decision, rule_id, latency_ms, upstream_ms, "
            "overhead_ms, is_error, result_summary, trace_id from audit_events where id = %s",
            (event_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == session_id
    assert row[1] == "place_order"
    assert row[2] == {"symbol": "AMD", "side": "buy", "notional": 500}
    assert row[3:5] == ("ALLOW", "pass_through")
    assert row[5:8] == (3.5, 2.0, 1.5)
    assert row[8] is False
    assert row[9] == '{"status": "new"}'
    assert row[10] == "0af7651916cd43dd8448eb211c80319c"


async def test_unreachable_database_fails_on_connect() -> None:
    bad = "postgresql://tradeguard:tradeguard@127.0.0.1:1/tradeguard?connect_timeout=2"
    with pytest.raises(psycopg.OperationalError):
        async with PostgresAuditSink(bad):
            pass


async def test_reader_filters_pages_and_counts_rules() -> None:
    from services.gateway.audit import AuditQuery, PostgresAuditReader

    migrate(database_url())
    session_id = f"pg-read-{uuid.uuid4().hex[:8]}"
    ids = []
    async with PostgresAuditSink(database_url()) as sink:
        for decision, rule in [("ALLOW", "pass"), ("DENY", "max_order_notional"),
                               ("ESCALATE", "approval_notional")]:  # fmt: skip
            ids.append(await sink.record_decision(DecisionRecord(
                session_id=session_id, upstream="replay", tool="place_order",
                args={"symbol": "NVDA"}, decision=decision, rule_id=rule,  # type: ignore[arg-type]
                reason="r",
            )))  # fmt: skip
    async with PostgresAuditReader(database_url()) as reader:
        events = await reader.list_events(AuditQuery(session_id=session_id))
        denied = await reader.list_events(AuditQuery(session_id=session_id, decision="DENY"))
        older = await reader.list_events(
            AuditQuery(session_id=session_id, limit=1, before_id=ids[2])
        )
        counts = await reader.rule_counts()
    assert [e["id"] for e in events] == list(reversed(ids))
    assert isinstance(events[0]["created_at"], str)
    assert events[0]["args"] == {"symbol": "NVDA"}
    assert [e["rule_id"] for e in denied] == ["max_order_notional"]
    assert [e["id"] for e in older] == [ids[1]]
    pairs = {(row["rule_id"], row["decision"]) for row in counts}
    assert ("max_order_notional", "DENY") in pairs
    assert all(row["decision"] != "ALLOW" for row in counts)


async def test_reader_counts_rules_for_one_session_prefix() -> None:
    from services.gateway.audit import PostgresAuditReader

    migrate(database_url())
    prefix = f"pg-prefix-{uuid.uuid4().hex[:6]}-"
    async with PostgresAuditSink(database_url()) as sink:
        await sink.record_decision(DecisionRecord(
            session_id=prefix + "a", upstream="replay", tool="place_order", args={},
            decision="DENY", rule_id="market_hours", reason="closed",
        ))  # fmt: skip
    async with PostgresAuditReader(database_url()) as reader:
        counts = await reader.rule_counts(session_prefix=prefix)
    assert counts == [{"rule_id": "market_hours", "decision": "DENY", "count": 1}]
