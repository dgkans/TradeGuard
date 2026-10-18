"""Chat sessions for the dashboard: the same agent and gateway as `tradeguard chat`.

Each session is one background task that owns its MCP connection. The MCP client must be
opened and closed in the same task, so HTTP requests never touch it directly; they hand
the session a job and wait for its result. One turn runs at a time per session.

Sessions live in this process only (in-memory checkpointer). They end when the API stops.
"""

import asyncio
import json
import logging
import secrets
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from mcp.client.stdio import StdioServerParameters

from services.agent.approvals import wait_for_decision
from services.agent.graph import AgentContext, build_graph, resume_turn, run_turn
from services.agent.provenance import report_user_message
from services.agent.tools import McpToolset, ToolCallLog
from services.api import catalog
from services.common.config import Settings, child_env
from services.common.llm import make_chat_model
from services.gateway.mcp_proxy.proxy import OPERATOR_META_KEY, OPERATOR_TOKEN_ENV

logger = logging.getLogger(__name__)
PREVIEW_CHARS = 400


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


Connector = Callable[[ChatSpec, str, str], AbstractAsyncContextManager[Any]]
ModelFactory = Callable[[], tuple[Any, Any]]
Job = tuple[Callable[[], Awaitable[Any]], "asyncio.Future[Any]"]


def preview(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 3] + "..."


class ChatSession:
    """One conversation. Use `start`, then `send` / `resume` / `advance`, then `close`."""

    def __init__(self, session_id: str, spec: ChatSpec, service: "ChatService") -> None:
        self.session_id = session_id
        self.spec = spec
        self.service = service
        self.transcript: list[dict[str, Any]] = []
        self.pending: dict[str, Any] | None = None
        self.log = ToolCallLog(thread_id=session_id)
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
        scenario = catalog.find_scenario(self.service.repo_root, self.spec.scenario) or {}
        context = AgentContext(self.spec.scenario, list(scenario.get("watchlist", [])))
        try:
            connect = self.service.connect(self.spec, self.session_id, self._operator_token)
            async with connect as target, McpToolset(target, log=self.log) as toolset:
                chat_model, json_model = self.service.models()
                self._toolset = toolset
                self._graph = build_graph(
                    chat_model, json_model, toolset.tools, context=context,
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

    async def send(self, text: str) -> dict[str, Any]:
        """Run one user message. Returns the turn (answer, or a pending approval)."""
        self._claim()
        if self.pending is not None:
            raise ChatBusyError("waiting for an approval decision: resume first")
        async with self._lock:
            self.transcript.append({"role": "user", "text": text})
            start = len(self.log.records)

            async def job() -> dict[str, Any]:
                if self.spec.guarded:
                    # Trusted provenance for the taint rule, sent by this code, not the model.
                    await report_user_message(self._toolset_or_fail(), text, self._meta)
                return await run_turn(self._graph, text, thread_id=self.session_id)

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
                return outcome, result

            outcome, result = await self._submit(job)
            self.pending = None
            self.transcript.append({"role": "decision", "outcome": outcome})
            turn = self._finish(result, start)
            turn["outcome"] = outcome
            return turn

    async def advance(self, minutes: int) -> dict[str, Any]:
        """Move the simulated clock (an operator action; the agent cannot do this)."""
        self._claim()
        async with self._lock:

            async def job() -> Any:
                return await self._toolset_or_fail().call(
                    "advance_clock", {"minutes": minutes}, self._meta
                )

            clock = await self._submit(job)
            self.transcript.append({"role": "clock", "minutes": minutes, "clock": clock})
            return {"clock": clock}

    def _toolset_or_fail(self) -> McpToolset:
        if self._toolset is None:
            raise ChatError("the session is not connected")
        return self._toolset

    def _finish(self, result: dict[str, Any], start: int) -> dict[str, Any]:
        calls = []
        for record in self.log.records[start:]:
            call = {
                "role": "tool",
                "tool": record["tool"],
                "args": record["args"],
                "error": record["error"],
                "latency_ms": record["latency_ms"],
                "result_preview": None if record["error"] else preview(record["result"]),
            }
            calls.append(call)
            self.transcript.append(call)
        pending = result.get("pending_approval")
        if pending is not None:
            if "args" not in pending:
                # The escalation body names the tool but not its arguments; show the order.
                for record in reversed(self.log.records):
                    if record["tool"] == pending.get("tool"):
                        pending = {**pending, "args": record["args"]}
                        break
            self.pending = pending
            self.transcript.append({"role": "approval", **pending})
        else:
            self.transcript.append({"role": "agent", "text": result.get("answer") or ""})
        return {
            "answer": result.get("answer"),
            "pending_approval": pending,
            "understand": {"request_type": result.get("request_type"),
                           "summary": result.get("summary")},
            "tool_calls": calls,
            "outcome": None,
        }  # fmt: skip

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.info(),
            "busy": self.busy,
            "pending_approval": self.pending,
            "transcript": self.transcript,
        }

    def info(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "scenario": self.spec.scenario,
            "guarded": self.spec.guarded,
            "policy": self.spec.policy if self.spec.guarded else None,
            "model": self.service.model_name,
        }

    async def close(self) -> None:
        if self._task is None:
            return
        await self._jobs.put(None)
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
        except TimeoutError:
            self._task.cancel()


class ChatService:
    """Creates and holds chat sessions. At most `max_sessions` open at once."""

    def __init__(
        self,
        connect: Connector,
        models: ModelFactory,
        approval_timeout_s: float,
        repo_root: Path,
        max_sessions: int = 5,
        max_tool_calls: int = 20,
        poll_seconds: float = 0.5,
        model_name: str = "",
    ) -> None:
        self.connect = connect
        self.models = models
        self.approval_timeout_s = approval_timeout_s
        self.repo_root = repo_root
        self.max_sessions = max_sessions
        self.max_tool_calls = max_tool_calls
        self.poll_seconds = poll_seconds
        self.model_name = model_name
        self.sessions: dict[str, ChatSession] = {}

    async def create(self, scenario: str, guarded: bool, policy: str) -> ChatSession:
        """Start a session. Raises ChatNotFoundError (scenario or policy), TooManySessions."""
        if catalog.find_scenario(self.repo_root, scenario) is None:
            raise ChatNotFoundError(f"no scenario {scenario!r} (held out ones are not offered)")
        path = catalog.policy_path(self.repo_root, policy)
        if path is None:
            raise ChatNotFoundError(f"no policy {policy!r}")
        if len(self.sessions) >= self.max_sessions:
            raise TooManySessionsError(f"at most {self.max_sessions} open sessions; close one")
        session_id = f"web-{scenario}-{uuid.uuid4().hex[:8]}"
        session = ChatSession(session_id, ChatSpec(scenario, guarded, policy, path), self)
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


def stdio_connector(repo_root: Path) -> Connector:
    """Real sessions: the gateway (or, direct, the replay server) as a stdio subprocess."""

    @asynccontextmanager
    async def connect(spec: ChatSpec, session_id: str, token: str) -> AsyncIterator[Any]:
        if spec.guarded:
            args = ["-m", "services.gateway", "--session", session_id, "--scenario",
                    spec.scenario, "--dev-tools", "--policy", str(spec.policy_path)]  # fmt: skip
        else:
            args = ["-m", "services.replay_server", "--scenario", spec.scenario, "--dev-tools"]
        env = child_env() | {OPERATOR_TOKEN_ENV: token}
        yield StdioServerParameters(command=sys.executable, args=args, cwd=repo_root, env=env)

    return connect


def settings_models(settings: Settings) -> ModelFactory:
    def models() -> tuple[Any, Any]:
        return make_chat_model(settings), make_chat_model(settings, json_mode=True)

    return models
