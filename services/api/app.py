"""Dashboard API: approvals, audit log, policies, eval reports and chat sessions.

    uv run tradeguard api                      # http://127.0.0.1:8000, docs at /docs

No authentication yet: it binds to 127.0.0.1 for local use. F12 adds auth before anything
is public. Paper/replay only, like everything else: chat sessions run replay scenarios.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.alpaca_adapter.adapter import PaperModeError, require_paper_settings
from services.api import catalog
from services.api.chat import ChatError, ChatService, settings_models, stdio_connector
from services.common.config import Settings
from services.gateway.api.app import add_approval_routes
from services.gateway.approvals import ApprovalStore, PostgresApprovalStore
from services.gateway.audit import AuditQuery, AuditReader, PostgresAuditReader

REPO_ROOT = Path(__file__).resolve().parents[2]
DECISIONS = ("ALLOW", "DENY", "ESCALATE")


@dataclass
class ApiDeps:
    approvals: ApprovalStore
    audit: AuditReader
    chat: ChatService | None = None
    repo_root: Path = REPO_ROOT
    jaeger_ui_url: str = "http://localhost:16686"


class ChatCreate(BaseModel):
    scenario: str = Field(min_length=1, max_length=100)
    guarded: bool = True
    policy: str = Field(default="default", min_length=1, max_length=100)


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ClockAdvance(BaseModel):
    minutes: int = Field(ge=1, le=390)  # at most one trading day


def create_app(deps: ApiDeps) -> FastAPI:
    """The dashboard API over the given stores. Errors are JSON `{"detail": ...}`."""
    app = FastAPI(title="TradeGuard API", version="0.2.0")

    @app.exception_handler(ChatError)
    async def chat_error(request: Request, exc: ChatError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    add_approval_routes(app, deps.approvals)
    add_audit_routes(app, deps)
    add_catalog_routes(app, deps.repo_root, deps.chat)
    add_chat_routes(app, deps)
    return app


def add_audit_routes(app: FastAPI, deps: ApiDeps) -> None:
    @app.get("/audit")
    async def audit(
        session_id: str | None = Query(default=None, max_length=200),
        decision: str | None = Query(default=None),
        tool: str | None = Query(default=None, max_length=100),
        limit: int = Query(default=100, ge=1, le=500),
        before_id: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        if decision is not None and decision not in DECISIONS:
            raise HTTPException(status_code=422, detail=f"decision must be one of {DECISIONS}")
        query = AuditQuery(session_id, decision, tool, limit, before_id)
        events = await deps.audit.list_events(query)
        base = deps.jaeger_ui_url.rstrip("/")
        for event in events:
            trace_id = event.get("trace_id")
            event["trace_url"] = f"{base}/trace/{trace_id}" if trace_id else None
        next_before = events[-1]["id"] if len(events) == limit else None
        if next_before is not None:
            older = await deps.audit.list_events(AuditQuery(session_id, decision, tool, 1,
                                                            int(next_before)))  # fmt: skip
            if not older:
                next_before = None
        return {"events": events, "next_before_id": next_before}

    @app.get("/rules/fired")
    async def rules_fired() -> dict[str, Any]:
        return {"rules": await deps.audit.rule_counts()}


def add_catalog_routes(app: FastAPI, root: Path, chat: ChatService | None = None) -> None:
    @app.get("/policies")
    async def policies() -> dict[str, Any]:
        return {"policies": catalog.list_policies(root)}

    @app.get("/policies/{name}")
    async def policy(name: str) -> dict[str, Any]:
        try:
            data = catalog.read_policy(root, name)
        except catalog.PolicyError as exc:
            raise HTTPException(status_code=500, detail=f"policy {name} is invalid: {exc}") from exc
        if data is None:
            raise HTTPException(status_code=404, detail=f"no policy {name}")
        return data

    @app.get("/evals")
    async def evals() -> dict[str, Any]:
        return {"reports": catalog.list_reports(root)}

    @app.get("/evals/latest")
    async def latest_eval() -> dict[str, Any]:
        found = catalog.latest_full_report(root)
        if found is None:
            raise HTTPException(status_code=404, detail="no full suite eval report yet")
        return {"name": found[0], "report": found[1]}

    @app.get("/evals/{name}")
    async def eval_report(name: str) -> dict[str, Any]:
        data = catalog.read_report(root, name)
        if data is None:
            raise HTTPException(status_code=404, detail=f"no eval report {name}")
        return {"name": name, "report": data}

    @app.get("/scenarios")
    async def scenarios() -> dict[str, Any]:
        offered = list(catalog.list_scenarios(root))
        paper = chat.paper_entry() if chat is not None else None
        if paper is not None:
            offered.insert(0, paper)
        return {"scenarios": offered}


def add_chat_routes(app: FastAPI, deps: ApiDeps) -> None:
    def service() -> ChatService:
        if deps.chat is None:
            raise HTTPException(status_code=503, detail="chat is not enabled on this server")
        return deps.chat

    @app.get("/chat/sessions")
    async def sessions() -> dict[str, Any]:
        out = []
        for session in service().sessions.values():
            out.append(session.info())
        return {"sessions": out}

    @app.post("/chat/sessions", status_code=201)
    async def create_session(body: ChatCreate) -> dict[str, Any]:
        session = await service().create(body.scenario, body.guarded, body.policy)
        return session.info()

    @app.get("/chat/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return service().get(session_id).snapshot()

    @app.post("/chat/sessions/{session_id}/messages")
    async def send(session_id: str, body: ChatMessage) -> dict[str, Any]:
        return await service().get(session_id).send(body.text)

    @app.post("/chat/sessions/{session_id}/resume")
    async def resume(session_id: str) -> dict[str, Any]:
        return await service().get(session_id).resume()

    @app.post("/chat/sessions/{session_id}/advance")
    async def advance(session_id: str, body: ClockAdvance) -> dict[str, Any]:
        return await service().get(session_id).advance(body.minutes)

    @app.delete("/chat/sessions/{session_id}", status_code=204)
    async def close(session_id: str) -> None:
        await service().close(session_id)


class _Late:
    """Stands in for a store that connects in the app's lifespan."""

    def __init__(self, holder: dict[str, Any], key: str) -> None:
        self._holder = holder
        self._key = key

    def __getattr__(self, name: str) -> Any:
        return getattr(self._holder[self._key], name)


def paper_watchlist(settings: Settings) -> list[str] | None:
    """The watchlist for paper sessions, or None when paper mode isn't fully configured."""
    try:
        require_paper_settings(settings)
    except PaperModeError:
        return None
    symbols = []
    for part in settings.paper_watchlist.split(","):
        if part.strip():
            symbols.append(part.strip().upper())
    return symbols


def create_postgres_app(settings: Settings) -> FastAPI:
    """The API against DATABASE_URL, with real chat sessions (gateway subprocesses)."""
    url = settings.database_url.get_secret_value()
    holder: dict[str, Any] = {}
    chat = ChatService(
        connect=stdio_connector(REPO_ROOT),
        models=settings_models(settings),
        approval_timeout_s=settings.approval_timeout_seconds,
        repo_root=REPO_ROOT,
        model_name=f"{settings.llm_provider}:{settings.llm_model}",
        paper_watchlist=paper_watchlist(settings),
    )
    deps = ApiDeps(
        approvals=_Late(holder, "approvals"),
        audit=_Late(holder, "audit"),
        chat=chat,
        jaeger_ui_url=settings.jaeger_ui_url,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with PostgresApprovalStore(url) as approvals, PostgresAuditReader(url) as audit:
            holder["approvals"] = approvals
            holder["audit"] = audit
            try:
                yield
            finally:
                await chat.close_all()

    app = create_app(deps)
    app.router.lifespan_context = lifespan
    return app
