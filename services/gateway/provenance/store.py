"""Session provenance: the ordered record of what entered the agent's context.

Entries are user messages (trusted, reported by the CLI's code through the operator
channel) and tool results (trusted or untrusted per the policy's trust labels). Untrusted
results carry the injection detector's score and signals. The gateway decides from the
in-memory copy and persists every entry, so a restarted gateway reloads the session.
"""

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.gateway.provenance.detector import detect

Kind = Literal["user_message", "tool_result"]
Trust = Literal["trusted", "untrusted"]
USER_TEXT_LIMIT = 4000
EXCERPT_LIMIT = 500


@dataclass(frozen=True)
class ProvenanceEntry:
    session_id: str
    seq: int
    kind: Kind
    source: str
    trust: Trust
    risk: float
    signals: list[str]
    text: str  # full user message, or an excerpt of a tool result
    sha256: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ProvenanceStore(Protocol):
    async def append(self, entry: ProvenanceEntry) -> None: ...

    async def load(self, session_id: str) -> list[ProvenanceEntry]: ...


class MemoryProvenanceStore:
    def __init__(self) -> None:
        self.entries: dict[str, list[ProvenanceEntry]] = {}

    async def append(self, entry: ProvenanceEntry) -> None:
        self.entries.setdefault(entry.session_id, []).append(entry)

    async def load(self, session_id: str) -> list[ProvenanceEntry]:
        return list(self.entries.get(session_id, []))


class PostgresProvenanceStore:
    """`provenance_events` table. Async context manager owning one connection."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._conn: psycopg.AsyncConnection[dict[str, Any]] | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "PostgresProvenanceStore":
        self._conn = await psycopg.AsyncConnection.connect(
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

    def _connection(self) -> psycopg.AsyncConnection[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("provenance store is not connected")
        return self._conn

    async def append(self, entry: ProvenanceEntry) -> None:
        async with self._lock:
            await self._connection().execute(
                "insert into provenance_events (session_id, seq, kind, source, trust, risk, "
                "signals, text, sha256, created_at) values (%s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s)",
                (entry.session_id, entry.seq, entry.kind, entry.source, entry.trust,
                 entry.risk, Jsonb(entry.signals), entry.text, entry.sha256, entry.created_at),
            )  # fmt: skip

    async def load(self, session_id: str) -> list[ProvenanceEntry]:
        async with self._lock:
            cursor = await self._connection().execute(
                "select session_id, seq, kind, source, trust, risk, signals, text, sha256, "
                "created_at from provenance_events where session_id = %s order by seq",
                (session_id,),
            )
            rows = await cursor.fetchall()
        return [ProvenanceEntry(**row) for row in rows]


class SessionProvenance:
    """In-memory provenance for one session, written through to a store."""

    def __init__(self, session_id: str, store: ProvenanceStore) -> None:
        self.session_id = session_id
        self.store = store
        self.entries: list[ProvenanceEntry] = []

    async def load(self) -> None:
        self.entries = await self.store.load(self.session_id)

    async def _append(
        self, kind: Kind, source: str, trust: Trust, text: str, scan: bool
    ) -> ProvenanceEntry:
        risk, signals = 0.0, []
        if scan:
            detection = detect(text)
            risk, signals = detection.score, detection.signals
        limit = USER_TEXT_LIMIT if kind == "user_message" else EXCERPT_LIMIT
        entry = ProvenanceEntry(
            session_id=self.session_id, seq=len(self.entries) + 1, kind=kind, source=source,
            trust=trust, risk=risk, signals=signals, text=text[:limit],
            sha256=hashlib.sha256(text.encode()).hexdigest(),
        )  # fmt: skip
        await self.store.append(entry)
        self.entries.append(entry)
        return entry

    async def record_user(self, text: str) -> ProvenanceEntry:
        return await self._append("user_message", "user", "trusted", text, scan=False)

    async def record_tool(self, tool: str, trust: Trust, text: str) -> ProvenanceEntry:
        """Record a tool result. Untrusted text is scanned (full text, not the excerpt)."""
        return await self._append("tool_result", tool, trust, text, scan=trust == "untrusted")

    def latest_user_text(self) -> str:
        for entry in reversed(self.entries):
            if entry.kind == "user_message":
                return entry.text
        return ""

    def untrusted(self) -> list[ProvenanceEntry]:
        return [entry for entry in self.entries if entry.trust == "untrusted"]

    def untrusted_seen(self) -> bool:
        return bool(self.untrusted())

    def max_risk(self) -> float:
        return max((entry.risk for entry in self.untrusted()), default=0.0)

    def signals(self) -> list[str]:
        found: list[str] = []
        for entry in self.untrusted():
            for signal in entry.signals:
                if signal not in found:
                    found.append(signal)
        return found
