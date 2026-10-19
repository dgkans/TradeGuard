"""Approval queue for escalated tool calls.

Lifecycle: pending -> approved | rejected | expired; approved -> executing -> executed | blocked.
- Expiry is applied before every read or decision, so a timed out approval can never be
  approved or executed ("timeout auto rejects").
- `claim` moves approved -> executing atomically, so a call runs at most once.
- Every human decision and every expiry also writes an `audit_events` row.
"""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Literal, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from services.common.pg import ResilientConnection
from services.gateway.audit import INSERT_DECISION, DecisionRecord, MemoryAuditSink

Status = Literal["pending", "approved", "rejected", "expired", "executing", "executed", "blocked"]
FINAL_FOR_HUMANS = ("approved", "rejected", "expired", "executing", "executed", "blocked")


class ApprovalNotFoundError(Exception):
    pass


class ApprovalConflictError(Exception):
    """The approval is no longer pending (already decided, expired or executed)."""


class Approval(BaseModel):
    id: str
    session_id: str
    tool: str
    args: dict[str, Any]
    rule_id: str
    reason: str
    status: Status
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str | None = None
    result: dict[str, Any] | None = None
    audit_event_id: int | None = None


class ApprovalStore(Protocol):
    async def create(
        self,
        *,
        session_id: str,
        tool: str,
        args: dict[str, Any],
        rule_id: str,
        reason: str,
        audit_event_id: int | None,
        timeout_s: float,
    ) -> Approval: ...

    async def get(self, approval_id: str) -> Approval | None: ...

    async def list(
        self, status: str | None = None, session_id: str | None = None
    ) -> list[Approval]: ...

    async def decide(
        self, approval_id: str, *, approve: bool, decided_by: str, note: str
    ) -> Approval: ...

    async def claim(self, approval_id: str) -> Approval | None: ...

    async def finish(
        self, approval_id: str, status: Literal["executed", "blocked"], result: dict[str, Any]
    ) -> Approval: ...


def new_id() -> str:
    return f"apr_{secrets.token_hex(6)}"


def decision_audit(approval: Approval, kind: str) -> DecisionRecord:
    """Audit row for a human decision or a timeout."""
    if kind == "approved":
        decision, rule_id = "ALLOW", "human_approval"
        reason = f"{approval.id} approved by {approval.decided_by}: {approval.note or ''}"
    elif kind == "rejected":
        decision, rule_id = "DENY", "human_rejection"
        reason = f"{approval.id} rejected by {approval.decided_by}: {approval.note or ''}"
    else:
        decision, rule_id = "DENY", "approval_timeout"
        reason = f"{approval.id} expired without a decision; auto rejected"
    return DecisionRecord(
        session_id=approval.session_id, upstream=None, tool=approval.tool, args=approval.args,
        decision=decision,  # type: ignore[arg-type]
        rule_id=rule_id, reason=reason.strip(),
    )  # fmt: skip


class MemoryApprovalStore:
    """In-process store for tests. Single event loop, so each method is atomic."""

    def __init__(self, audit: MemoryAuditSink | None = None) -> None:
        self.audit = audit
        self.items: dict[str, Approval] = {}

    async def _audit(self, approval: Approval, kind: str) -> None:
        if self.audit is not None:
            await self.audit.record_decision(decision_audit(approval, kind))

    async def _expire(self, approval: Approval) -> Approval:
        now = datetime.now(UTC)
        if approval.status == "pending" and approval.expires_at <= now:
            approval = approval.model_copy(
                update={"status": "expired", "decided_at": now, "decided_by": "timeout"}
            )
            self.items[approval.id] = approval
            await self._audit(approval, "expired")
        return approval

    async def create(
        self,
        *,
        session_id: str,
        tool: str,
        args: dict[str, Any],
        rule_id: str,
        reason: str,
        audit_event_id: int | None,
        timeout_s: float,
    ) -> Approval:
        now = datetime.now(UTC)
        approval = Approval(
            id=new_id(), session_id=session_id, tool=tool, args=dict(args), rule_id=rule_id,
            reason=reason, status="pending", created_at=now,
            expires_at=now + timedelta(seconds=timeout_s), audit_event_id=audit_event_id,
        )  # fmt: skip
        self.items[approval.id] = approval
        return approval

    async def get(self, approval_id: str) -> Approval | None:
        approval = self.items.get(approval_id)
        if approval is None:
            return None
        return await self._expire(approval)

    async def list(
        self, status: str | None = None, session_id: str | None = None
    ) -> list[Approval]:
        rows = []
        for approval_id in list(self.items):
            approval = await self._expire(self.items[approval_id])
            if status is not None and approval.status != status:
                continue
            if session_id is not None and approval.session_id != session_id:
                continue
            rows.append(approval)
        return rows

    async def decide(
        self, approval_id: str, *, approve: bool, decided_by: str, note: str
    ) -> Approval:
        approval = await self.get(approval_id)
        if approval is None:
            raise ApprovalNotFoundError(approval_id)
        if approval.status != "pending":
            raise ApprovalConflictError(f"{approval_id} is {approval.status}, not pending")
        status: Status = "approved" if approve else "rejected"
        changes = {"status": status, "decided_by": decided_by, "note": note,
                   "decided_at": datetime.now(UTC)}  # fmt: skip
        approval = approval.model_copy(update=changes)
        self.items[approval_id] = approval
        await self._audit(approval, status)
        return approval

    async def claim(self, approval_id: str) -> Approval | None:
        approval = await self.get(approval_id)
        if approval is None or approval.status != "approved":
            return None
        approval = approval.model_copy(update={"status": "executing"})
        self.items[approval_id] = approval
        return approval

    async def finish(
        self, approval_id: str, status: Literal["executed", "blocked"], result: dict[str, Any]
    ) -> Approval:
        approval = self.items[approval_id].model_copy(update={"status": status, "result": result})
        self.items[approval_id] = approval
        return approval


COLUMNS = (
    "id, session_id, tool, args, rule_id, reason, status, created_at, expires_at, "
    "decided_at, decided_by, note, result, audit_event_id"
)
# Built only from constant column names and a fixed scope clause.
EXPIRE_DUE = f"""
update approvals set status = 'expired', decided_at = now(), decided_by = 'timeout'
where status = 'pending' and expires_at <= now() {{scope}}
returning {COLUMNS}
"""


class PostgresApprovalStore:
    """`approvals` table store. Async context manager owning one connection.

    State changes and their audit rows commit in one transaction.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._conn: ResilientConnection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "PostgresApprovalStore":
        self._conn = await ResilientConnection.connect(
            self.url, autocommit=True, row_factory=dict_row
        )
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
            raise RuntimeError("approval store is not connected")
        return self._conn

    async def _expire(self, approval_id: str | None) -> None:
        """Expire due approvals (one, or all) and audit each, in the current transaction."""
        conn = self._connection()
        if approval_id is None:
            cursor = await conn.execute(EXPIRE_DUE.format(scope=""))
        else:
            cursor = await conn.execute(EXPIRE_DUE.format(scope="and id = %s"), (approval_id,))
        for row in await cursor.fetchall():
            await self._insert_audit(decision_audit(Approval(**row), "expired"))

    async def _insert_audit(self, record: DecisionRecord) -> None:
        await self._connection().execute(
            INSERT_DECISION,
            (record.session_id, record.upstream, record.tool, Jsonb(record.args),
             record.decision, record.rule_id, record.reason, record.trace_id, record.span_id),
        )  # fmt: skip

    async def _fetch(self, approval_id: str, lock: bool = False) -> Approval | None:
        suffix = " for update" if lock else ""
        cursor = await self._connection().execute(
            f"select {COLUMNS} from approvals where id = %s{suffix}",
            (approval_id,),
        )
        row = await cursor.fetchone()
        return Approval(**row) if row else None

    async def create(
        self,
        *,
        session_id: str,
        tool: str,
        args: dict[str, Any],
        rule_id: str,
        reason: str,
        audit_event_id: int | None,
        timeout_s: float,
    ) -> Approval:
        async with self._lock:
            cursor = await self._connection().execute(
                f"insert into approvals (id, session_id, tool, args, rule_id, reason, "
                f"expires_at, audit_event_id) values (%s, %s, %s, %s, %s, %s, "
                f"now() + make_interval(secs => %s), %s) returning {COLUMNS}",
                (new_id(), session_id, tool, Jsonb(args), rule_id, reason, timeout_s,
                 audit_event_id),
            )  # fmt: skip
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("approval insert returned nothing")
        return Approval(**row)

    async def get(self, approval_id: str) -> Approval | None:
        async with self._lock, self._connection().transaction():
            await self._expire(approval_id)
            return await self._fetch(approval_id)

    async def list(
        self, status: str | None = None, session_id: str | None = None
    ) -> list[Approval]:
        clauses = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if session_id is not None:
            clauses.append("session_id = %s")
            params.append(session_id)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        async with self._lock, self._connection().transaction():
            await self._expire(None)
            cursor = await self._connection().execute(
                f"select {COLUMNS} from approvals {where} order by created_at",
                params,
            )
            rows = await cursor.fetchall()
        return [Approval(**row) for row in rows]

    async def decide(
        self, approval_id: str, *, approve: bool, decided_by: str, note: str
    ) -> Approval:
        status = "approved" if approve else "rejected"
        async with self._lock, self._connection().transaction():
            await self._expire(approval_id)
            current = await self._fetch(approval_id, lock=True)
            if current is None:
                raise ApprovalNotFoundError(approval_id)
            if current.status != "pending":
                raise ApprovalConflictError(f"{approval_id} is {current.status}, not pending")
            cursor = await self._connection().execute(
                f"update approvals set status = %s, decided_by = %s, note = %s, "
                f"decided_at = now() where id = %s returning {COLUMNS}",
                (status, decided_by, note, approval_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ApprovalNotFoundError(approval_id)
            approval = Approval(**row)
            await self._insert_audit(decision_audit(approval, status))
        return approval

    async def claim(self, approval_id: str) -> Approval | None:
        async with self._lock, self._connection().transaction():
            await self._expire(approval_id)
            cursor = await self._connection().execute(
                f"update approvals set status = 'executing' where id = %s "
                f"and status = 'approved' returning {COLUMNS}",
                (approval_id,),
            )
            row = await cursor.fetchone()
        return Approval(**row) if row else None

    async def finish(
        self, approval_id: str, status: Literal["executed", "blocked"], result: dict[str, Any]
    ) -> Approval:
        async with self._lock:
            cursor = await self._connection().execute(
                f"update approvals set status = %s, result = %s where id = %s returning {COLUMNS}",
                (status, Jsonb(result), approval_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise ApprovalNotFoundError(approval_id)
        return Approval(**row)
