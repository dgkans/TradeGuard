"""Dashboard API: trader sessions, positions and limits, market data, and an owner-only
admin area (approvals, audit log, eval reports).

    uv run tradeguard api                      # http://127.0.0.1:8000, docs at /docs

Traders don't log in (user decision, F10d); the admin area needs ADMIN_PASSWORD
(services/api/admin.py). The API binds to 127.0.0.1 for local use. Paper/replay only:
trader sessions run replay scenarios.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.alpaca_adapter.adapter import PaperModeError, require_paper_settings
from services.api import catalog, market
from services.api.admin import AdminAuth, AdminDisabledError, AdminLockedError, AdminLoginError
from services.api.chat import (
    ChatError,
    ChatService,
    GatewayStores,
    inprocess_connector,
    settings_models,
)
from services.api.describe import RULE_TITLES
from services.api.limits import Limits
from services.common.config import Settings
from services.common.llm import make_chat_model
from services.gateway.api.app import add_approval_routes
from services.gateway.approvals import (
    ApprovalConflictError,
    ApprovalStore,
    PostgresApprovalStore,
)
from services.gateway.audit import (
    AuditQuery,
    AuditReader,
    PostgresAuditReader,
    PostgresAuditSink,
    check_schema,
)
from services.gateway.intent.judge import LlmJudge
from services.gateway.policy.model import load_policy
from services.gateway.provenance.store import PostgresProvenanceStore

REPO_ROOT = Path(__file__).resolve().parents[2]
DECISIONS = ("ALLOW", "DENY", "ESCALATE")


@dataclass
class ApiDeps:
    approvals: ApprovalStore
    audit: AuditReader
    chat: ChatService | None = None
    repo_root: Path = REPO_ROOT
    jaeger_ui_url: str = "http://localhost:16686"
    admin: AdminAuth = field(default_factory=lambda: AdminAuth(None))  # off unless configured


class ChatCreate(BaseModel):
    user: str = Field(default="trader", min_length=1, max_length=40)
    scenario: str = Field(default="desk", min_length=1, max_length=100)
    guarded: bool = True
    policy: str = Field(default="default", min_length=1, max_length=100)
    starting_cash: float = Field(default=10000, ge=1000, le=1_000_000)


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class AdminLogin(BaseModel):
    password: str = Field(max_length=200)


class TraderDecision(BaseModel):
    note: str = Field(default="", max_length=2000)


def create_app(deps: ApiDeps) -> FastAPI:
    """The dashboard API over the given stores. Errors are JSON `{"detail": ...}`."""
    app = FastAPI(title="TradeGuard API", version="0.2.0")

    @app.exception_handler(ChatError)
    async def chat_error(request: Request, exc: ChatError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    async def require_admin(authorization: str | None = Header(default=None)) -> None:
        token = (authorization or "").removeprefix("Bearer ").strip()
        if not deps.admin.verify(token):
            raise HTTPException(status_code=401, detail="admin login required")

    admin_only = [Depends(require_admin)]

    @app.post("/admin/login")
    async def admin_login(body: AdminLogin) -> dict[str, str]:
        try:
            issued = deps.admin.login(body.password)
        except AdminDisabledError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AdminLockedError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except AdminLoginError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return {"token": issued.token, "expires_at": issued.expires_at}

    @app.get("/admin/me", dependencies=admin_only)
    async def admin_me() -> dict[str, bool]:
        return {"admin": True}

    @app.get("/market/overview")
    async def market_overview() -> dict[str, Any]:
        return market.overview(deps.repo_root)

    add_approval_routes(app, deps.approvals, dependencies=admin_only)
    add_audit_routes(app, deps, admin_only)
    add_catalog_routes(app, deps.repo_root, deps.chat, admin_only)
    add_chat_routes(app, deps)
    return app


def add_audit_routes(app: FastAPI, deps: ApiDeps, admin_only: list[Any]) -> None:
    @app.get("/audit", dependencies=admin_only)
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

    @app.get("/rules/fired", dependencies=admin_only)
    async def rules_fired(
        scope: str | None = Query(default=None, pattern="^traders$"),
    ) -> dict[str, Any]:
        """Blocked and held counts per rule; `scope=traders` counts dashboard sessions only
        (not CLI, eval or test runs that share the database)."""
        prefix = "web-" if scope == "traders" else None
        return {"rules": titled(await deps.audit.rule_counts(session_prefix=prefix))}


def titled(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for rule in rules:
        rule["title"] = RULE_TITLES.get(str(rule["rule_id"]), str(rule["rule_id"]))
    return rules


def add_catalog_routes(
    app: FastAPI, root: Path, chat: ChatService | None = None, admin_only: list[Any] | None = None
) -> None:
    guarded = admin_only or []

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

    @app.get("/evals", dependencies=guarded)
    async def evals() -> dict[str, Any]:
        return {"reports": catalog.list_reports(root)}

    @app.get("/evals/latest", dependencies=guarded)
    async def latest_eval(
        agent: str | None = Query(default=None, pattern="^attacker$"),
    ) -> dict[str, Any]:
        """Newest main suite run with the LLM agent, or with `agent=attacker` the scripted
        worst case agent's run (F9c). The two answer different questions."""
        found = catalog.latest_full_report(root, agent=agent)
        if found is None:
            raise HTTPException(status_code=404, detail="no full suite eval report yet")
        return {"name": found[0], "report": found[1]}

    @app.get("/evals/{name}", dependencies=guarded)
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
        session = await service().create(
            body.scenario, body.guarded, body.policy, body.user, body.starting_cash
        )
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

    @app.get("/chat/sessions/{session_id}/portfolio")
    async def portfolio(session_id: str) -> dict[str, Any]:
        return service().get(session_id).portfolio()

    async def decide_own(session_id: str, approval_id: str, approve: bool,
                         body: TraderDecision) -> Any:  # fmt: skip
        """A trader decides an approval only if it belongs to their own session."""
        session = service().get(session_id)
        approval = await deps.approvals.get(approval_id)
        if approval is None or approval.session_id != session_id:
            raise HTTPException(status_code=404, detail="no such approval in this session")
        try:
            return await deps.approvals.decide(
                approval_id, approve=approve, decided_by=session.spec.user, note=body.note
            )
        except ApprovalConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/chat/sessions/{session_id}/approvals/{approval_id}/approve")
    async def approve_own(session_id: str, approval_id: str, body: TraderDecision) -> Any:
        return await decide_own(session_id, approval_id, True, body)

    @app.post("/chat/sessions/{session_id}/approvals/{approval_id}/reject")
    async def reject_own(session_id: str, approval_id: str, body: TraderDecision) -> Any:
        return await decide_own(session_id, approval_id, False, body)

    @app.get("/chat/sessions/{session_id}/limits")
    async def get_limits(
        session_id: str, template: str | None = Query(default=None, max_length=100)
    ) -> dict[str, Any]:
        """The trader's current limits, or a profile template's (Standard, Strict)."""
        session = service().get(session_id)
        if template is None:
            return session.limits()
        path = catalog.policy_path(deps.repo_root, template)
        if path is None:
            raise HTTPException(status_code=404, detail=f"no profile {template}")
        return session.limits(load_policy(path))

    @app.put("/chat/sessions/{session_id}/limits")
    async def put_limits(session_id: str, body: Limits) -> dict[str, Any]:
        return await service().get(session_id).set_limits(body)

    @app.get("/chat/sessions/{session_id}/stopped")
    async def stopped(session_id: str) -> dict[str, Any]:
        """What the guard blocked or held for this trader (their session only)."""
        service().get(session_id)
        return {"rules": titled(await deps.audit.rule_counts(session_prefix=session_id))}

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
    """The API against DATABASE_URL. Trader sessions run their market and gateway in this
    process and share one set of Postgres stores (F10b); paper sessions use a subprocess."""
    url = settings.database_url.get_secret_value()
    holder: dict[str, Any] = {}

    def stores() -> GatewayStores:
        return cast(GatewayStores, holder["stores"])

    chat = ChatService(
        connect=inprocess_connector(REPO_ROOT, stores, settings.approval_timeout_seconds),
        models=settings_models(settings),
        approval_timeout_s=settings.approval_timeout_seconds,
        repo_root=REPO_ROOT,
        model_name=f"{settings.llm_provider}:{settings.llm_model}",
        paper_watchlist=paper_watchlist(settings),
    )
    password = settings.admin_password.get_secret_value() if settings.admin_password else None
    deps = ApiDeps(
        approvals=_Late(holder, "approvals"),
        audit=_Late(holder, "audit"),
        chat=chat,
        jaeger_ui_url=settings.jaeger_ui_url,
        admin=AdminAuth(password),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await check_schema(url)
        async with AsyncExitStack() as stack:
            approvals = await stack.enter_async_context(PostgresApprovalStore(url))
            reader = await stack.enter_async_context(PostgresAuditReader(url))
            sink = await stack.enter_async_context(PostgresAuditSink(url))
            provenance = await stack.enter_async_context(PostgresProvenanceStore(url))
            judge_model = make_chat_model(settings, json_mode=True, model=settings.judge_model)
            judge = LlmJudge(judge_model, model=f"{settings.llm_provider}:{settings.judge_model}")
            holder["approvals"] = approvals
            holder["audit"] = reader
            holder["stores"] = GatewayStores(sink, approvals, provenance, judge)
            try:
                yield
            finally:
                await chat.close_all()

    app = create_app(deps)
    app.router.lifespan_context = lifespan
    return app
