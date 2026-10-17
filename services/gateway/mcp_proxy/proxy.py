"""TradeGuard gateway: an MCP server to the agent and an MCP client to each upstream.

With a policy, every call goes through the decision pipeline (services/gateway/pipeline).
Without one (tests of the pure proxy), known tools pass through unchanged. Any failure
before forwarding is a DENY, and only ALLOW is ever forwarded.
"""

import asyncio
import json
import math
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any

import mcp_types as types
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.mcpserver import MCPServer
from opentelemetry import trace

from services.common import config
from services.common.telemetry import current_ids
from services.gateway.audit import AuditOutcome, AuditSink, DecisionRecord, summarize_result
from services.gateway.pipeline.pipeline import Pipeline
from services.gateway.pipeline.types import PolicyContext, ToolCall, Verdict
from services.gateway.policy.model import Policy
from services.replay_server.bars import MARKET_TZ

UpstreamTarget = MCPServer | Server[Any] | StdioServerParameters | str
tracer = trace.get_tracer("tradeguard.gateway")
# Key in MCP request `_meta` carrying the session's operator token. Set by the CLI's own
# code (for example /advance), never by the model, which only chooses tool name and args.
OPERATOR_META_KEY = "tradeguard/operator_token"
# The CLI hands the token to the gateway in this env var (not argv, so it stays out of
# process listings).
OPERATOR_TOKEN_ENV = config.OPERATOR_TOKEN_ENV
ESCALATE_NOTE = (
    "This call needs human approval. It was not executed. Tell the user it is waiting for "
    "approval and why."
)


class GatewayConfigError(Exception):
    """The gateway cannot start safely (for example, two upstreams expose the same tool)."""


@dataclass(frozen=True)
class Upstream:
    name: str
    target: UpstreamTarget


class Gateway:
    """Async context manager. Connects to every upstream, then serves `self.server`."""

    def __init__(
        self,
        upstreams: list[Upstream],
        *,
        audit: AuditSink,
        session_id: str,
        policy: Policy | None = None,
        operator_token: str | None = None,
    ) -> None:
        self.upstreams = upstreams
        self.audit = audit
        self.session_id = session_id
        self.policy = policy
        self.operator_token = operator_token
        self.pipeline: Pipeline | None = None
        self.tools: list[types.Tool] = []
        self._routes: dict[str, tuple[str, Client]] = {}
        self._timings: list[tuple[float, float]] = []  # (latency_ms, upstream_ms) per call
        self._stack = AsyncExitStack()
        self.server: Server[Any] = Server(
            "tradeguard-gateway",
            instructions="TradeGuard safety gateway. Paper and replay trading only.",
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    async def __aenter__(self) -> "Gateway":
        try:
            for upstream in self.upstreams:
                client = await self._stack.enter_async_context(Client(upstream.target))
                listed = await client.list_tools()
                for tool in listed.tools:
                    if tool.name in self._routes:
                        other = self._routes[tool.name][0]
                        raise GatewayConfigError(
                            f"tool {tool.name!r} is exposed by both {other!r} and "
                            f"{upstream.name!r}; refusing to start"
                        )
                    self._routes[tool.name] = (upstream.name, client)
                    self.tools.append(tool)
        except BaseException:
            await self._stack.aclose()
            raise
        if self.policy is not None:
            schemas = {}
            for tool in self.tools:
                schemas[tool.name] = dict(tool.input_schema)
            self.pipeline = Pipeline(self.policy, schemas)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._stack.aclose()

    # --- MCP handlers ---------------------------------------------------------------

    async def _list_tools(
        self, ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list(self.tools))

    async def _call_tool(
        self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        meta = params.meta or {}
        token = meta.get(OPERATOR_META_KEY) if isinstance(meta, dict) else None
        return await self.handle(params.name, dict(params.arguments or {}), operator_token=token)

    # --- pipeline -------------------------------------------------------------------

    async def decide(
        self, tool: str, args: dict[str, Any], operator_token: object = None
    ) -> Verdict:
        """Unknown tool -> DENY. Operator tool with the right token -> ALLOW. No policy ->
        pass through. Otherwise the policy pipeline decides."""
        if tool not in self._routes:
            return Verdict("DENY", "unknown_tool", f"no upstream exposes tool {tool!r}")
        if self.is_operator_call(tool, operator_token):
            return Verdict("ALLOW", "operator", "operator tool called with the session token")
        if self.pipeline is None:
            return Verdict("ALLOW", "pass_through", "no policy configured: pass through")
        return await self.pipeline.decide(ToolCall(tool, args), self.fetch_context)

    def is_operator_call(self, tool: str, token: object) -> bool:
        if self.policy is None or self.operator_token is None:
            return False
        return tool in self.policy.operator_tools and token == self.operator_token

    async def fetch_context(self, call: ToolCall) -> PolicyContext:
        """Read account and market state from the upstream. These reads are the gateway's
        own and are not audited as agent calls. Raises if any read fails."""
        symbol = str(call.arg("symbol", "")).strip().upper()
        quote, positions, account, clock, orders = await asyncio.gather(
            self._read("get_quote", {"symbol": symbol}),
            self._read("get_positions", {}),
            self._read("get_account", {}),
            self._read("get_clock", {}),
            self._read("list_orders", {"status": "all"}),
        )
        held = 0.0
        for position in positions["positions"]:
            if position["symbol"] == symbol:
                held = float(position["qty"])
        today = datetime.fromisoformat(clock["now"]).astimezone(MARKET_TZ).date()
        orders_today = 0
        for order in orders["orders"]:
            submitted = datetime.fromisoformat(order["submitted_at"]).astimezone(MARKET_TZ)
            if submitted.date() == today and order["status"] != "rejected":
                orders_today += 1
        return PolicyContext(
            price=float(quote["price"]),
            held_qty=held,
            positions_value=float(account["positions_value"]),
            equity=float(account["equity"]),
            market_open=bool(clock["market_open"]),
            orders_today=orders_today,
        )

    async def _read(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool not in self._routes:
            raise RuntimeError(f"no upstream provides {tool}, needed for policy context")
        result = await self._routes[tool][1].call_tool(tool, args)
        if result.is_error or not isinstance(result.structured_content, dict):
            raise RuntimeError(f"{tool} failed: {result_text(result)}")
        return result.structured_content

    async def handle(
        self, tool: str, args: dict[str, Any], operator_token: object = None
    ) -> types.CallToolResult:
        """Decide, audit, forward. Fails closed: if the decision cannot be recorded, DENY."""
        started = time.perf_counter()
        attributes = {"tradeguard.tool": tool, "tradeguard.session_id": self.session_id}
        with tracer.start_as_current_span("gateway.decision", attributes=attributes) as span:
            verdict = await self.decide(tool, args, operator_token)
            span.set_attribute("tradeguard.decision", verdict.decision)
            span.set_attribute("tradeguard.rule_id", verdict.rule_id)
            upstream_name = self._routes[tool][0] if tool in self._routes else None
            trace_id, span_id = current_ids()
            record = DecisionRecord(
                session_id=self.session_id, upstream=upstream_name, tool=tool, args=args,
                decision=verdict.decision, rule_id=verdict.rule_id, reason=verdict.reason,
                trace_id=trace_id, span_id=span_id,
            )  # fmt: skip
            try:
                event_id = await self.audit.record_decision(record)
            except Exception as exc:
                span.set_attribute("tradeguard.decision", "DENY")
                span.set_attribute("tradeguard.rule_id", "audit_unavailable")
                failed = Verdict("DENY", "audit_unavailable", f"audit log write failed: {exc}")
                return self.refusal(tool, failed)

            upstream_ms = 0.0
            if verdict.decision != "ALLOW":
                result = self.refusal(tool, verdict)
            else:
                result, upstream_ms = await self._forward(tool, args)

            latency_ms = (time.perf_counter() - started) * 1000
            text = result_text(result)
            outcome = AuditOutcome(
                latency_ms=latency_ms,
                upstream_ms=upstream_ms,
                is_error=bool(result.is_error),
                result_summary=summarize_result(result.structured_content, text),
            )
            span.set_attribute("tradeguard.overhead_ms", outcome.overhead_ms)
            span.set_attribute("tradeguard.upstream_ms", upstream_ms)
            if verdict.decision == "ALLOW":
                self._timings.append((latency_ms, upstream_ms))
            try:
                await self.audit.record_outcome(event_id, outcome)
            except Exception as exc:
                # The call already ran upstream. Don't hide its result; flag the gap.
                span.set_attribute("tradeguard.audit_outcome_failed", str(exc))
            return result

    async def _forward(self, tool: str, args: dict[str, Any]) -> tuple[types.CallToolResult, float]:
        upstream_name, client = self._routes[tool]
        started = time.perf_counter()
        with tracer.start_as_current_span(
            "gateway.upstream_call", attributes={"tradeguard.upstream": upstream_name}
        ):
            try:
                result = await client.call_tool(tool, args)
            except Exception as exc:
                result = refusal(f"upstream {upstream_name} failed: {type(exc).__name__}: {exc}")
        return result, (time.perf_counter() - started) * 1000

    def refusal(self, tool: str, verdict: Verdict) -> types.CallToolResult:
        """Structured DENY or ESCALATE for the agent, as JSON text so it can explain it."""
        body: dict[str, Any] = {
            "decision": verdict.decision,
            "rule_id": verdict.rule_id,
            "reason": verdict.reason,
            "tool": tool,
            "policy": self.policy.name if self.policy else None,
            "executed": False,
        }
        if verdict.decision == "ESCALATE":
            body["note"] = ESCALATE_NOTE
        return refusal(json.dumps(body))

    # --- stats ----------------------------------------------------------------------

    def overhead_stats(self) -> dict[str, float]:
        """p50 and p95 of gateway overhead and upstream time over forwarded calls so far."""
        overheads = []
        upstreams = []
        for latency, upstream in self._timings:
            overheads.append(max(latency - upstream, 0.0))
            upstreams.append(upstream)
        return {
            "calls": float(len(self._timings)),
            "overhead_p50_ms": percentile(overheads, 50),
            "overhead_p95_ms": percentile(overheads, 95),
            "upstream_p50_ms": percentile(upstreams, 50),
            "upstream_p95_ms": percentile(upstreams, 95),
        }


def refusal(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)],
                                is_error=True)  # fmt: skip


def result_text(result: types.CallToolResult) -> str:
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return " ".join(parts)


def percentile(values: list[float], pct: float) -> float:
    """Nearest rank percentile. 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(math.ceil(pct / 100 * len(ordered)), 1)
    return round(ordered[rank - 1], 3)
