"""Audit trail for gateway decisions.

Each call is written twice: the decision *before* the call is forwarded (if that write
fails, the gateway denies the call), then the outcome after the upstream returns.
"""

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb

from services.common.pg import ResilientConnection

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
        event["created_at"] = datetime.now(UTC).isoformat()
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
        self._conn: ResilientConnection | None = None
        # One connection cannot run two queries at once.
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "PostgresAuditSink":
        self._conn = await ResilientConnection.connect(self.url, autocommit=True)
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

    def _connection(self) -> ResilientConnection:
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


# --- reading the trail (dashboard) ---------------------------------------------------

EVENT_FIELDS = (
    "id", "created_at", "session_id", "upstream", "tool", "args", "decision", "rule_id",
    "reason", "latency_ms", "upstream_ms", "overhead_ms", "is_error", "result_summary",
    "trace_id", "span_id",
)  # fmt: skip


@dataclass(frozen=True)
class AuditQuery:
    """Filters for reading audit events, newest first. `before_id` pages to older events."""

    session_id: str | None = None
    decision: str | None = None
    tool: str | None = None
    limit: int = 100
    before_id: int | None = None


class AuditReader(Protocol):
    async def list_events(self, query: AuditQuery) -> list[dict[str, Any]]:
        """Events matching `query`, newest first, at most `query.limit`."""
        ...

    async def rule_counts(self, session_prefix: str | None = None) -> list[dict[str, Any]]:
        """How often each rule blocked or escalated: [{rule_id, decision, count}], most first.
        `session_prefix` limits it to sessions whose id starts with it (e.g. "web-")."""
        ...


def _matches(event: dict[str, Any], query: AuditQuery) -> bool:
    if query.session_id is not None and event.get("session_id") != query.session_id:
        return False
    if query.decision is not None and event.get("decision") != query.decision:
        return False
    if query.tool is not None and event.get("tool") != query.tool:
        return False
    return query.before_id is None or int(event["id"]) < query.before_id


def _sorted_counts(counts: dict[tuple[str, str], int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for (rule_id, decision), count in counts.items():
        rows.append({"rule_id": rule_id, "decision": decision, "count": count})
    # Most fired first; ties by rule id so the order is stable.
    rows.sort(key=lambda row: (-row["count"], row["rule_id"]))
    return rows


class MemoryAuditReader:
    """Reads a MemoryAuditSink (tests and in-process runs)."""

    def __init__(self, sink: MemoryAuditSink) -> None:
        self.sink = sink

    async def list_events(self, query: AuditQuery) -> list[dict[str, Any]]:
        out = []
        for event in reversed(self.sink.events):
            if not _matches(event, query):
                continue
            row = {}
            for field in EVENT_FIELDS:
                row[field] = event.get(field)
            out.append(row)
            if len(out) >= query.limit:
                break
        return out

    async def rule_counts(self, session_prefix: str | None = None) -> list[dict[str, Any]]:
        counts: dict[tuple[str, str], int] = {}
        for event in self.sink.events:
            if event["decision"] == "ALLOW":
                continue
            if session_prefix and not str(event["session_id"]).startswith(session_prefix):
                continue
            key = (event["rule_id"], event["decision"])
            counts[key] = counts.get(key, 0) + 1
        return _sorted_counts(counts)


SELECT_EVENTS = f"""
select {", ".join(EVENT_FIELDS)} from audit_events
where (%(session_id)s::text is null or session_id = %(session_id)s)
  and (%(decision)s::text is null or decision = %(decision)s)
  and (%(tool)s::text is null or tool = %(tool)s)
  and (%(before_id)s::bigint is null or id < %(before_id)s)
order by id desc
limit %(limit)s
"""

SELECT_RULE_COUNTS = """
select rule_id, decision, count(*) from audit_events
where decision <> 'ALLOW'
  and (%(prefix)s::text is null or starts_with(session_id, %(prefix)s))
group by rule_id, decision
"""


class PostgresAuditReader:
    """Reads `audit_events`. Async context manager owning one read connection."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._conn: ResilientConnection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "PostgresAuditReader":
        self._conn = await ResilientConnection.connect(self.url, autocommit=True)
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

    def _connection(self) -> ResilientConnection:
        if self._conn is None:
            raise RuntimeError("audit reader is not connected")
        return self._conn

    async def list_events(self, query: AuditQuery) -> list[dict[str, Any]]:
        params = asdict(query)
        async with self._lock:
            cursor = await self._connection().execute(SELECT_EVENTS, params)
            rows = await cursor.fetchall()
        out = []
        for row in rows:
            event = dict(zip(EVENT_FIELDS, row, strict=True))
            if isinstance(event["created_at"], datetime):
                event["created_at"] = event["created_at"].isoformat()
            out.append(event)
        return out

    async def rule_counts(self, session_prefix: str | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._connection().execute(
                SELECT_RULE_COUNTS, {"prefix": session_prefix}
            )
            rows = await cursor.fetchall()
        counts: dict[tuple[str, str], int] = {}
        for rule_id, decision, count in rows:
            counts[(str(rule_id), str(decision))] = int(count)
        return _sorted_counts(counts)


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
            "select to_regclass('public.audit_events'), to_regclass('public.approvals'), "
            "to_regclass('public.provenance_events')"
        )
        row = await cursor.fetchone()
    if row is None or None in row:
        raise RuntimeError("a TradeGuard table is missing: run `make migrate`")


def summarize_result(structured: Any, text: str, limit: int = 500) -> str:
    """Short, single line description of a tool result for the audit log."""
    if structured is not None:
        summary = json.dumps(structured, default=str, separators=(",", ":"))
    else:
        summary = text
    if len(summary) > limit:
        return summary[: limit - 3] + "..."
    return summary
