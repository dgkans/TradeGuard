"""Audit trail for gateway decisions.

Each call is written twice: the decision *before* the call is forwarded (if that write
fails, the gateway denies the call), then the outcome after the upstream returns.
"""

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb

Decision = Literal["ALLOW", "DENY", "ESCALATE"]
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DecisionRecord:
    session_id: str
    upstream: str | None
    tool: str
    args: dict[str, Any]
    decision: Decision
    rule_id: str
    reason: str
    trace_id: str | None = None
    span_id: str | None = None


@dataclass(frozen=True)
class AuditOutcome:
    latency_ms: float  # whole gateway handling time, before the outcome write
    upstream_ms: float  # time spent waiting on the upstream server (0 if not forwarded)
    is_error: bool
    result_summary: str

    @property
    def overhead_ms(self) -> float:
        return max(self.latency_ms - self.upstream_ms, 0.0)


class AuditSink(Protocol):
    async def record_decision(self, record: DecisionRecord) -> int:
        """Store a decision and return its event id. Raising means: do not forward."""
        ...

    async def record_outcome(self, event_id: int, outcome: AuditOutcome) -> None: ...


class MemoryAuditSink:
    """In-process sink for tests and `--audit memory` runs. Not for production."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record_decision(self, record: DecisionRecord) -> int:
        event = asdict(record)
        event["id"] = len(self.events) + 1
        self.events.append(event)
        return int(event["id"])

    async def record_outcome(self, event_id: int, outcome: AuditOutcome) -> None:
        event = self.events[event_id - 1]
        event.update(asdict(outcome))
        event["overhead_ms"] = outcome.overhead_ms


INSERT_DECISION = """
insert into audit_events
    (session_id, upstream, tool, args, decision, rule_id, reason, trace_id, span_id)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
returning id
"""

UPDATE_OUTCOME = """
update audit_events
set latency_ms = %s, upstream_ms = %s, overhead_ms = %s, is_error = %s, result_summary = %s
where id = %s
"""


class PostgresAuditSink:
    """Writes to the `audit_events` table. Async context manager owning one connection.

    Failure behavior: connecting raises psycopg.OperationalError; a failed write raises,
    and the gateway turns that into a DENY.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._conn: psycopg.AsyncConnection[Any] | None = None
        # One connection cannot run two queries at once.
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "PostgresAuditSink":
        self._conn = await psycopg.AsyncConnection.connect(self.url, autocommit=True)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _connection(self) -> psycopg.AsyncConnection[Any]:
        if self._conn is None:
            raise RuntimeError("audit sink is not connected")
        return self._conn

    async def record_decision(self, record: DecisionRecord) -> int:
        async with self._lock:
            cursor = await self._connection().execute(
                INSERT_DECISION,
                (record.session_id, record.upstream, record.tool, Jsonb(record.args),
                 record.decision, record.rule_id, record.reason, record.trace_id,
                 record.span_id),
            )  # fmt: skip
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("audit insert returned no id")
        return int(row[0])

    async def record_outcome(self, event_id: int, outcome: AuditOutcome) -> None:
        async with self._lock:
            await self._connection().execute(
                UPDATE_OUTCOME,
                (outcome.latency_ms, outcome.upstream_ms, outcome.overhead_ms,
                 outcome.is_error, outcome.result_summary, event_id),
            )  # fmt: skip


def migrate(url: str) -> None:
    """Apply all Alembic migrations to the database at `url`."""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


async def check_schema(url: str) -> None:
    """Fail at startup, not on the first call, when a table is missing."""
    async with await psycopg.AsyncConnection.connect(url) as conn:
        cursor = await conn.execute(
            "select to_regclass('public.audit_events'), to_regclass('public.approvals')"
        )
        row = await cursor.fetchone()
    if row is None or row[0] is None or row[1] is None:
        raise RuntimeError("audit_events or approvals table missing: run `make migrate`")


def summarize_result(structured: Any, text: str, limit: int = 500) -> str:
    """Short, single line description of a tool result for the audit log."""
    if structured is not None:
        summary = json.dumps(structured, default=str, separators=(",", ":"))
    else:
        summary = text
    if len(summary) > limit:
        return summary[: limit - 3] + "..."
    return summary
