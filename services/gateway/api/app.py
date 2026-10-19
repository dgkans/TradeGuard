"""Approval API: list escalated calls and approve or reject them.

    uv run tradeguard api                      # http://127.0.0.1:8000
    curl -s localhost:8000/approvals?status=pending
    curl -s -X POST localhost:8000/approvals/<id>/approve -H 'content-type: application/json' \\
         -d '{"decided_by": "alice", "note": "ok"}'

No authentication yet: local use only. F12 adds auth before anything is public.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from services.gateway.approvals import (
    Approval,
    ApprovalConflictError,
    ApprovalNotFoundError,
    ApprovalStore,
    PostgresApprovalStore,
)


class DecisionBody(BaseModel):
    decided_by: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=2000)


class ApprovalList(BaseModel):
    approvals: list[Approval]


def create_app(store: ApprovalStore) -> FastAPI:
    """FastAPI app over an approval store. Errors: 404 unknown id, 409 not pending."""
    app = FastAPI(title="TradeGuard approvals", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    add_approval_routes(app, store)
    return app


def add_approval_routes(
    app: FastAPI, store: ApprovalStore, dependencies: Sequence[Any] = ()
) -> None:
    """Mount the approval endpoints on `app` (also used by the dashboard API, which passes
    its admin check in `dependencies`)."""
    guarded = list(dependencies)

    @app.get("/approvals", dependencies=guarded)
    async def list_approvals(
        status: str | None = Query(default=None), session_id: str | None = Query(default=None)
    ) -> ApprovalList:
        return ApprovalList(approvals=await store.list(status=status, session_id=session_id))

    @app.get("/approvals/{approval_id}", dependencies=guarded)
    async def get_approval(approval_id: str) -> Approval:
        approval = await store.get(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail=f"no approval {approval_id}")
        return approval

    async def decide(approval_id: str, approve: bool, body: DecisionBody) -> Approval:
        try:
            return await store.decide(
                approval_id, approve=approve, decided_by=body.decided_by, note=body.note
            )
        except ApprovalNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"no approval {approval_id}") from exc
        except ApprovalConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/approvals/{approval_id}/approve", dependencies=guarded)
    async def approve(approval_id: str, body: DecisionBody) -> Approval:
        return await decide(approval_id, True, body)

    @app.post("/approvals/{approval_id}/reject", dependencies=guarded)
    async def reject(approval_id: str, body: DecisionBody) -> Approval:
        return await decide(approval_id, False, body)


def create_postgres_app(url: str) -> FastAPI:
    """App whose store connects on startup and closes on shutdown."""
    holder: dict[str, Any] = {}

    class LazyStore:
        def __getattr__(self, name: str) -> Any:
            return getattr(holder["store"], name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with PostgresApprovalStore(url) as store:
            holder["store"] = store
            yield

    app = create_app(LazyStore())
    app.router.lifespan_context = lifespan
    return app
