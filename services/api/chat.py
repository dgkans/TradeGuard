"""Trader sessions for the dashboard: the same agent and gateway as `tradeguard chat`.

Each session belongs to one trader and is one background task that owns its MCP
connection (the MCP client must be opened and closed in the same task). HTTP requests
never touch it directly; they hand the session a job and wait for the result. One turn
runs at a time per session; different traders' sessions run side by side.

Replay sessions run their market and their gateway inside the API process (F10b): no
subprocesses per trader. After each turn the session's market moves forward 5 minutes, so
orders fill without a clock button. Sessions live in this process only (in-memory
checkpointer) and end when the API stops.
"""

import asyncio
import logging
import re
import secrets
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from mcp.client.stdio import StdioServerParameters

from services.agent.approvals import wait_for_decision
from services.agent.graph import AgentContext, build_graph, resume_turn, run_turn
from services.agent.provenance import report_user_message
from services.agent.tools import McpToolset, ToolCallLog
from services.api import catalog
from services.api.describe import (
    RULE_TITLES,
    describe_decision,
    describe_step,
    friendly_reason,
)
from services.common.config import Settings, child_env
from services.common.llm import make_chat_model
from services.gateway.mcp_proxy.proxy import OPERATOR_META_KEY, OPERATOR_TOKEN_ENV

logger = logging.getLogger(__name__)
PAPER_SCENARIO = "alpaca_paper"
DEFAULT_SCENARIO = "desk"
FILL_MINUTES = 5
# Trader sessions only: the CLI and the evals keep the plain system prompt.
TRADER_STYLE = (
    "This chat is shown to a retail trader in an app. Answer in short, plain sentences. "
    "Never show order ids, tool names, JSON or field names. Market orders fill within a "
    "few minutes, so after placing one say it was sent, not filled, unless get_positions "
    "or list_orders shows it filled. Only state positions, cash or P&L you just read with "
    "a tool."
)


class ChatError(Exception):
    """A chat request that cannot be served. `status` is the HTTP status to answer with."""

    status = 400


class ChatNotFoundError(ChatError):
    status = 404


class ChatBusyError(ChatError):
    status = 409


class TooManySessionsError(ChatError):
    status = 429


class ChatStartError(ChatError):
    status = 502


@dataclass(frozen=True)
class ChatSpec:
    """What a session runs: a scenario, through the gateway (with a policy) or direct."""

    scenario: str
    guarded: bool
    policy: str
    policy_path: Path
    user: str = "trader"
    starting_cash: float = 10000.0


@dataclass
class Connection:
    """What a connector hands a session: the agent's MCP target, and the market it trades
    in when that runs in process (a ReplaySession), for positions and automatic fills."""

    target: Any
    market: Any | None = None


Connector = Callable[[ChatSpec, str, str], AbstractAsyncContextManager[Connection]]
ModelFactory = Callable[[], tuple[Any, Any]]
Job = tuple[Callable[[], Awaitable[Any]], "asyncio.Future[Any]"]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "trader"


class ChatSession:
    """One trader's conversation. `start`, then `send` / `resume`, then `close`."""

    def __init__(self, session_id: str, spec: ChatSpec, service: "ChatService") -> None:
        self.session_id = session_id
        self.spec = spec
        self.service = service
        self.started_at = datetime.now(UTC).isoformat()
        self.transcript: list[dict[str, Any]] = []
        self.pending: dict[str, Any] | None = None
        self.log = ToolCallLog(thread_id=session_id)
        self.market: Any | None = None
        self._operator_token = secrets.token_urlsafe(24)
        self._meta = {OPERATOR_META_KEY: self._operator_token}
        self._jobs: asyncio.Queue[Job | None] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._toolset: McpToolset | None = None
        self._graph: Any = None
        self._task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def start(self) -> None:
        """Connect and build the agent. Raises ChatStartError if that fails."""
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._work(ready), name=f"chat-{self.session_id}")
        try:
            await ready
        except Exception as exc:
            raise ChatStartError(f"could not start the session: {exc}") from exc

    async def _work(self, ready: "asyncio.Future[None]") -> None:
        scenario = self.service.find_scenario(self.spec.scenario) or {}
        context = AgentContext(
            self.spec.scenario, list(scenario.get("watchlist", [])), style=TRADER_STYLE
        )
        try:
            connect = self.service.connect(self.spec, self.session_id, self._operator_token)
            async with connect as connection, McpToolset(connection.target, log=self.log) as ts:
                chat_model, json_model = self.service.models()
                self._toolset = ts
                self.market = connection.market
                self._graph = build_graph(
                    chat_model, json_model, ts.tools, context=context,
                    checkpointer=InMemorySaver(), max_tool_calls=self.service.max_tool_calls,
                )  # fmt: skip
                ready.set_result(None)
                await self._serve()
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                logger.exception("chat session %s stopped", self.session_id)
        finally:
            self._fail_queued()

    async def _serve(self) -> None:
        while True:
            job = await self._jobs.get()
            if job is None:
                return
            func, future = job
            try:
                result = await func()
            except Exception as exc:  # reported to the waiting request, session stays up
                if not future.done():
                    future.set_exception(exc)
                continue
            if not future.done():
                future.set_result(result)

    def _fail_queued(self) -> None:
        while not self._jobs.empty():
            job = self._jobs.get_nowait()
            if job is not None and not job[1].done():
                job[1].set_exception(ChatError("the session has stopped"))

    async def _submit(self, func: Callable[[], Awaitable[Any]]) -> Any:
        if self._task is None or self._task.done():
            raise ChatError("the session has stopped")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._jobs.put((func, future))
        return await future

    def _claim(self) -> None:
        if self._lock.locked():
            raise ChatBusyError("this session is already running a turn")

    def _toolset_or_fail(self) -> McpToolset:
        if self._toolset is None:
            raise ChatError("the session is not connected")
        return self._toolset

    def _let_orders_fill(self) -> None:
        """The demo market moves on after a turn, so open orders can fill (no clock button)."""
        if self.market is None:
            return
        open_orders = self.market.list_orders("open")["orders"]
        if open_orders:
            self.market.advance(FILL_MINUTES)

    async def send(self, text: str) -> dict[str, Any]:
        """Run one user message. Returns the turn (answer, or a pending approval)."""
        self._claim()
        if self.pending is not None:
            raise ChatBusyError("waiting for an approval decision: approve or reject it first")
        async with self._lock:
            self.transcript.append({"role": "user", "text": text})
            start = len(self.log.records)

            async def job() -> dict[str, Any]:
                if self.spec.guarded:
                    # Trusted provenance for the taint rule, sent by this code, not the model.
                    await report_user_message(self._toolset_or_fail(), text, self._meta)
                result = await run_turn(self._graph, text, thread_id=self.session_id)
                if result.get("pending_approval") is None:
                    self._let_orders_fill()
                return result

            result = await self._submit(job)
            return self._finish(result, start)

    async def resume(self) -> dict[str, Any]:
        """Wait for the human decision on the pending approval, then finish the turn."""
        self._claim()
        pending = self.pending
        if pending is None:
            raise ChatBusyError("nothing is waiting for approval")
        async with self._lock:
            start = len(self.log.records)
            wait_s = self.service.approval_timeout_s + 30

            async def job() -> tuple[dict[str, Any], dict[str, Any]]:
                toolset = self._toolset_or_fail()
                outcome = await wait_for_decision(
                    toolset, pending["approval_id"], self._meta,
                    poll_seconds=self.service.poll_seconds, max_wait_seconds=wait_s,
                )  # fmt: skip
                result = await resume_turn(self._graph, outcome, thread_id=self.session_id)
                if result.get("pending_approval") is None:
                    self._let_orders_fill()
                return outcome, result

            outcome, result = await self._submit(job)
            self.pending = None
            decision = {"role": "decision", **describe_decision(outcome)}
            self.transcript.append(decision)
            turn = self._finish(result, start)
            turn["decision"] = decision
            return turn

    def _finish(self, result: dict[str, Any], start: int) -> dict[str, Any]:
        steps = []
        for record in self.log.records[start:]:
            step = {"role": "step",
                    **describe_step(record["tool"], record["args"], record["result"],
                                    record["error"])}  # fmt: skip
            if step["status"] == "held":
                continue  # shown as the approval card instead
            steps.append(step)
            self.transcript.append(step)
        pending = result.get("pending_approval")
        if pending is not None:
            args = pending.get("args")
            if not args:
                # The escalation body names the tool but not its arguments; show the order.
                for record in reversed(self.log.records):
                    if record["tool"] == pending.get("tool"):
                        args = record["args"]
                        break
            card = {
                "approval_id": pending["approval_id"],
                "tool": pending.get("tool"),
                "args": args or {},
                "rule_id": pending.get("rule_id"),
                "title": RULE_TITLES.get(str(pending.get("rule_id")), "Needs your approval"),
                "reason": friendly_reason(str(pending.get("reason") or "")),
                "expires_at": pending.get("expires_at"),
            }
            self.pending = card
            self.transcript.append({"role": "approval", **card})
        else:
            self.transcript.append({"role": "agent", "text": result.get("answer") or ""})
        return {
            "answer": result.get("answer"),
            "pending_approval": self.pending if pending is not None else None,
            "steps": steps,
            "decision": None,
        }

    def portfolio(self) -> dict[str, Any]:
        """Positions with LTP, P&L and day change, cash and equity, and today's orders."""
        if self.market is None:
            raise ChatError("positions are only available for replay sessions in this version")
        market = self.market
        account = market.get_account()
        rows = []
        total_pnl = 0.0
        total_day = 0.0
        for position in market.get_positions()["positions"]:
            symbol = position["symbol"]
            qty = float(position["qty"])
            ltp = float(position["market_price"])
            avg = float(position["avg_entry_price"])
            previous = previous_close(market, symbol)
            day_change = (ltp - previous) / previous * 100 if previous else None
            pnl = (ltp - avg) * qty
            total_pnl += pnl
            if previous:
                total_day += (ltp - previous) * qty
            rows.append({
                "symbol": symbol, "qty": qty, "avg": avg, "ltp": ltp,
                "value": ltp * qty, "pnl": pnl, "pnl_pct": (ltp - avg) / avg * 100 if avg else 0,
                "day_change_pct": day_change,
            })  # fmt: skip
        orders = []
        for order in reversed(market.list_orders("all")["orders"]):
            orders.append({
                "id": order["id"], "symbol": order["symbol"], "side": order["side"],
                "type": order["type"], "qty": order["qty"], "notional": order["notional"],
                "limit_price": order["limit_price"], "status": order["status"],
                "submitted_at": order["submitted_at"], "filled_qty": order["filled_qty"],
                "filled_avg_price": order["filled_avg_price"],
            })  # fmt: skip
        clock = market.get_clock()
        return {
            "as_of": clock["now"],
            "market_open": clock["market_open"],
            "cash": account["cash"],
            "equity": account["equity"],
            "positions_value": account["positions_value"],
            "positions": rows,
            "total_pnl": total_pnl,
            "total_day_change": total_day,
            "orders": orders,
        }

    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user": self.spec.user,
            "scenario": self.spec.scenario,
            "guarded": self.spec.guarded,
            "policy": self.spec.policy if self.spec.guarded else None,
            "model": self.service.model_name,
            "started_at": self.started_at,
            "starting_cash": self.spec.starting_cash,
            "busy": self.busy,
            "waiting_for_approval": self.pending is not None,
        }

    def snapshot(self) -> dict[str, Any]:
        return {**self.info(), "pending_approval": self.pending, "transcript": self.transcript}

    async def close(self) -> None:
        if self._task is None:
            return
        await self._jobs.put(None)
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
        except TimeoutError:
            self._task.cancel()


def previous_close(market: Any, symbol: str) -> float | None:
    """Close of the last full trading day before now (for the day change column)."""
    try:
        bars = market.get_bars(symbol, "1d", 1)["bars"]
    except Exception:  # a symbol without daily bars simply has no day change
        return None
    return float(bars[-1]["close"]) if bars else None


class ChatService:
    """Creates and holds trader sessions. At most `max_sessions` open at once, one per trader."""

    def __init__(
        self,
        connect: Connector,
        models: ModelFactory,
        approval_timeout_s: float,
        repo_root: Path,
        max_sessions: int = 8,
        max_tool_calls: int = 20,
        poll_seconds: float = 0.5,
        model_name: str = "",
        paper_watchlist: list[str] | None = None,
    ) -> None:
        self.connect = connect
        self.models = models
        self.approval_timeout_s = approval_timeout_s
        self.repo_root = repo_root
        self.max_sessions = max_sessions
        self.max_tool_calls = max_tool_calls
        self.poll_seconds = poll_seconds
        self.model_name = model_name
        self.paper_watchlist = paper_watchlist
        self.sessions: dict[str, ChatSession] = {}

    def paper_entry(self) -> dict[str, Any] | None:
        """The Alpaca paper "scenario", offered only when paper mode is configured."""
        if not self.paper_watchlist:
            return None
        return {
            "id": PAPER_SCENARIO,
            "category": "paper",
            "description": "Your Alpaca PAPER account: live market data, paper money",
            "watchlist": list(self.paper_watchlist),
            "start_time": "now",
            "path": "",
        }

    def find_scenario(self, scenario_id: str) -> dict[str, Any] | None:
        if scenario_id == PAPER_SCENARIO:
            return self.paper_entry()
        return catalog.find_scenario(self.repo_root, scenario_id)

    async def create(
        self,
        scenario: str,
        guarded: bool,
        policy: str,
        user: str = "trader",
        starting_cash: float = 10000.0,
    ) -> ChatSession:
        """Start a session. Raises ChatNotFoundError (scenario or policy), ChatBusyError (the
        trader already has one), TooManySessionsError."""
        user = " ".join(user.split())
        if not user:
            raise ChatError("enter a trader name")
        if self.find_scenario(scenario) is None:
            raise ChatNotFoundError(f"no scenario {scenario!r} (held out ones are not offered)")
        if scenario == PAPER_SCENARIO and not guarded:
            raise ChatError("a paper account session always goes through the gateway")
        path = catalog.policy_path(self.repo_root, policy)
        if path is None:
            raise ChatNotFoundError(f"no policy {policy!r}")
        for existing in self.sessions.values():
            if existing.spec.user.lower() == user.lower():
                raise ChatBusyError(f"{user} already has an open session; end it first")
        if len(self.sessions) >= self.max_sessions:
            raise TooManySessionsError(f"at most {self.max_sessions} open sessions; end one")
        session_id = f"web-{slug(user)}-{uuid.uuid4().hex[:8]}"
        spec = ChatSpec(scenario, guarded, policy, path, user, float(starting_cash))
        session = ChatSession(session_id, spec, self)
        await session.start()
        self.sessions[session_id] = session
        return session

    def get(self, session_id: str) -> ChatSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise ChatNotFoundError(f"no session {session_id}")
        return session

    async def close(self, session_id: str) -> None:
        session = self.get(session_id)
        del self.sessions[session_id]
        await session.close()

    async def close_all(self) -> None:
        for session_id in list(self.sessions):
            await self.close(session_id)


@dataclass
class GatewayStores:
    """The stores every in-process gateway shares (Postgres in production)."""

    audit: Any
    approvals: Any
    provenance: Any
    judge: Any | None = None


def inprocess_connector(
    repo_root: Path, stores: Callable[[], GatewayStores], approval_timeout_s: float
) -> Connector:
    """Replay sessions with their market and gateway inside this process (F10b)."""
    from services.gateway.mcp_proxy.proxy import Gateway, Upstream
    from services.gateway.policy.model import load_policy
    from services.replay_server.scenario import load_scenario
    from services.replay_server.server import build_server
    from services.replay_server.session import ReplaySession

    paper = stdio_connector(repo_root)

    @asynccontextmanager
    async def connect(spec: ChatSpec, session_id: str, token: str) -> AsyncIterator[Connection]:
        if spec.scenario == PAPER_SCENARIO:
            async with paper(spec, session_id, token) as connection:
                yield connection
            return
        # A new trader's account: the cash they chose and no positions (F10c).
        scenario = load_scenario(spec.scenario, repo_root / "scenarios").model_copy(
            update={"starting_cash": spec.starting_cash, "positions": {}}
        )
        market = ReplaySession(
            scenario,
            bars_root=repo_root / "fixtures/bars",
            news_root=repo_root / "fixtures/news",
        )
        upstream = build_server(market, dev_tools=True)
        if not spec.guarded:
            yield Connection(upstream, market)
            return
        shared = stores()
        policy = load_policy(spec.policy_path)
        judge = shared.judge if policy.intent.enabled and policy.intent.judge.enabled else None
        gateway = Gateway(
            [Upstream("replay", upstream)], audit=shared.audit, session_id=session_id,
            policy=policy, operator_token=token, approvals=shared.approvals,
            approval_timeout_s=approval_timeout_s, provenance=shared.provenance, judge=judge,
        )  # fmt: skip
        async with gateway:
            yield Connection(gateway.server, market)

    return connect


def stdio_connector(repo_root: Path) -> Connector:
    """Sessions whose gateway runs as a stdio subprocess (Alpaca paper; the F10 design)."""

    @asynccontextmanager
    async def connect(spec: ChatSpec, session_id: str, token: str) -> AsyncIterator[Connection]:
        if spec.scenario == PAPER_SCENARIO:
            if not spec.guarded:
                raise ChatError("a paper account session always goes through the gateway")
            args = ["-m", "services.gateway", "--session", session_id, "--upstream", "alpaca",
                    "--policy", str(spec.policy_path)]  # fmt: skip
        elif spec.guarded:
            args = ["-m", "services.gateway", "--session", session_id, "--scenario",
                    spec.scenario, "--dev-tools", "--policy", str(spec.policy_path)]  # fmt: skip
        else:
            args = ["-m", "services.replay_server", "--scenario", spec.scenario, "--dev-tools"]
        env = child_env() | {OPERATOR_TOKEN_ENV: token}
        params = StdioServerParameters(command=sys.executable, args=args, cwd=repo_root, env=env)
        yield Connection(params, None)

    return connect


def settings_models(settings: Settings) -> ModelFactory:
    def models() -> tuple[Any, Any]:
        return make_chat_model(settings), make_chat_model(settings, json_mode=True)

    return models
