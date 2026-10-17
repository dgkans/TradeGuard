"""TradeGuard gateway: an MCP server to the agent and an MCP client to each upstream.

F4 forwards every known tool call unchanged (decision ALLOW, rule `pass_through`). The
policy pipeline (F5 onward) plugs in at `decide()`. Any failure before forwarding is a DENY.
"""

import math
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import mcp_types as types
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.mcpserver import MCPServer
from opentelemetry import trace

from services.common.telemetry import current_ids
from services.gateway.audit import AuditOutcome, AuditSink, DecisionRecord, summarize_result

UpstreamTarget = MCPServer | Server[Any] | StdioServerParameters | str
tracer = trace.get_tracer("tradeguard.gateway")


class GatewayConfigError(Exception):
    """The gateway cannot start safely (for example, two upstreams expose the same tool)."""


@dataclass(frozen=True)
class Upstream:
    name: str
    target: UpstreamTarget


@dataclass(frozen=True)
class Verdict:
    decision: str
    rule_id: str
    reason: str


class Gateway:
    """Async context manager. Connects to every upstream, then serves `self.server`."""

    def __init__(self, upstreams: list[Upstream], *, audit: AuditSink, session_id: str) -> None:
        self.upstreams = upstreams
        self.audit = audit
        self.session_id = session_id
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
        return await self.handle(params.name, dict(params.arguments or {}))

    # --- pipeline -------------------------------------------------------------------

    def decide(self, tool: str, args: dict[str, Any]) -> Verdict:
        """Pass through for every known tool. F5 replaces this with the policy pipeline."""
        if tool not in self._routes:
            return Verdict("DENY", "unknown_tool", f"no upstream exposes tool {tool!r}")
        return Verdict("ALLOW", "pass_through", "F4 pass through: no policy configured")

    async def handle(self, tool: str, args: dict[str, Any]) -> types.CallToolResult:
        """Decide, audit, forward. Fails closed: if the decision cannot be recorded, DENY."""
        started = time.perf_counter()
        attributes = {"tradeguard.tool": tool, "tradeguard.session_id": self.session_id}
        with tracer.start_as_current_span("gateway.decision", attributes=attributes) as span:
            verdict = self.decide(tool, args)
            span.set_attribute("tradeguard.decision", verdict.decision)
            span.set_attribute("tradeguard.rule_id", verdict.rule_id)
            upstream_name = self._routes[tool][0] if tool in self._routes else None
            trace_id, span_id = current_ids()
            record = DecisionRecord(
                session_id=self.session_id, upstream=upstream_name, tool=tool, args=args,
                decision=verdict.decision,  # type: ignore[arg-type]
                rule_id=verdict.rule_id, reason=verdict.reason,
                trace_id=trace_id, span_id=span_id,
            )  # fmt: skip
            try:
                event_id = await self.audit.record_decision(record)
            except Exception as exc:
                span.set_attribute("tradeguard.decision", "DENY")
                span.set_attribute("tradeguard.rule_id", "audit_unavailable")
                return refusal(f"DENY (audit_unavailable): audit log write failed: {exc}")

            upstream_ms = 0.0
            if verdict.decision != "ALLOW":
                result = refusal(f"{verdict.decision} ({verdict.rule_id}): {verdict.reason}")
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
