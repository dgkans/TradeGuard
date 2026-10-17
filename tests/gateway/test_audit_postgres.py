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
