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
from services.gateway.approvals import Approval, ApprovalStore
from services.gateway.audit import AuditOutcome, AuditSink, DecisionRecord, summarize_result
from services.gateway.intent.judge import Judge
from services.gateway.pipeline.pipeline import Pipeline
from services.gateway.pipeline.types import PolicyContext, ToolCall, Verdict
from services.gateway.policy.model import Policy
from services.gateway.provenance.store import (
    MemoryProvenanceStore,
    ProvenanceStore,
    SessionProvenance,
)
from services.gateway.provenance.wrap import wrap_untrusted
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
# Gateway native operator tool. Listed (so MCP clients don't re-list tools and warn on
# every call), hidden from the model by the agent's toolset, and refused without the token.
RESOLVE_TOOL = "resolve_approval"
RESOLVE_TOOL_SPEC = types.Tool(
    name=RESOLVE_TOOL,
    description="OPERATOR ONLY. Report an approval's state; execute it once if approved.",
    input_schema={
        "type": "object",
        "properties": {"approval_id": {"type": "string"}},
        "required": ["approval_id"],
    },
)
DEFAULT_APPROVAL_TIMEOUT_S = 300.0
# Gateway native operator tool: the CLI's code reports each user message (trusted
# provenance). Listed but hidden from the model; refused without the operator token.
RECORD_USER_TOOL = "record_user_message"
RECORD_USER_TOOL_SPEC = types.Tool(
    name=RECORD_USER_TOOL,
    description="OPERATOR ONLY. Record the user's message for provenance tracking.",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)
OPERATOR_ONLY_NATIVE = (RESOLVE_TOOL, RECORD_USER_TOOL)


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
        approvals: ApprovalStore | None = None,
        approval_timeout_s: float = DEFAULT_APPROVAL_TIMEOUT_S,
        provenance: ProvenanceStore | None = None,
        judge: Judge | None = None,
    ) -> None:
        self.upstreams = upstreams
        self.audit = audit
        self.session_id = session_id
        self.policy = policy
        self.operator_token = operator_token
        self.approvals = approvals
        self.approval_timeout_s = approval_timeout_s
        self.pipeline: Pipeline | None = None
        self.judge = judge
        self.provenance: SessionProvenance | None = None
        if policy is not None and (policy.taint.enabled or policy.intent.enabled):
            # Without an explicit store, keep provenance in this process only.
            self.provenance = SessionProvenance(session_id, provenance or MemoryProvenanceStore())
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
        if self.provenance is not None:
            await self.provenance.load()  # a restarted gateway resumes the session's record
        if self.policy is not None:
            schemas = {}
            for tool in self.tools:
                schemas[tool.name] = dict(tool.input_schema)
            self.pipeline = Pipeline(self.policy, schemas, judge=self.judge)
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
        tools = list(self.tools)
        if self.approvals is not None:
            tools.append(RESOLVE_TOOL_SPEC)
        if self.provenance is not None:
            tools.append(RECORD_USER_TOOL_SPEC)
        return types.ListToolsResult(tools=tools)

    async def _call_tool(
        self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        meta = params.meta or {}
        token = meta.get(OPERATOR_META_KEY) if isinstance(meta, dict) else None
        args = dict(params.arguments or {})
        if params.name == RESOLVE_TOOL and self.approvals is not None and self._operator_ok(token):
            return await self.resolve(str(args.get("approval_id", "")))
        recording = params.name == RECORD_USER_TOOL and self.provenance is not None
        if recording and self._operator_ok(token):
            return await self.record_user(str(args.get("text", "")))
        return await self.handle(params.name, args, operator_token=token)

    # --- pipeline -------------------------------------------------------------------

    async def decide(
        self, tool: str, args: dict[str, Any], operator_token: object = None
    ) -> Verdict:
        """Unknown tool -> DENY. Operator tool with the right token -> ALLOW. No policy ->
        pass through. Otherwise the policy pipeline decides."""
        if tool in OPERATOR_ONLY_NATIVE:
            return Verdict("DENY", "operator_only", f"{tool} needs the session operator token")
        if tool not in self._routes:
            return Verdict("DENY", "unknown_tool", f"no upstream exposes tool {tool!r}")
        if self.is_operator_call(tool, operator_token):
            return Verdict("ALLOW", "operator", "operator tool called with the session token")
        if self.pipeline is None:
            return Verdict("ALLOW", "pass_through", "no policy configured: pass through")
        return await self.pipeline.decide(
            ToolCall(tool, args), self.fetch_context, provenance=self.provenance
        )

    def _operator_ok(self, token: object) -> bool:
        return self.operator_token is not None and token == self.operator_token

    def is_operator_call(self, tool: str, token: object) -> bool:
        if self.policy is None:
            return False
        return tool in self.policy.operator_tools and self._operator_ok(token)

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
            for key, value in verdict.attributes.items():
                span.set_attribute(key, value)
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
            if verdict.decision == "ESCALATE" and self.approvals is not None:
                result = await self._park(tool, args, verdict, event_id)
            elif verdict.decision != "ALLOW":
                result = self.refusal(tool, verdict)
            else:
                result, upstream_ms = await self._forward(tool, args)
                result = await self._label(tool, result)

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

    # --- provenance -----------------------------------------------------------------

    async def record_user(self, text: str) -> types.CallToolResult:
        """Operator only: record the user's message as trusted provenance."""
        if self.provenance is None:
            return refusal("provenance tracking is off")
        entry = await self.provenance.record_user(text)
        body = {"recorded": True, "seq": entry.seq}
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body))])

    async def _label(self, tool: str, result: types.CallToolResult) -> types.CallToolResult:
        """Record a forwarded result in provenance and wrap it if it is untrusted."""
        if self.provenance is None or self.policy is None or result.is_error:
            return result
        trust = self.policy.taint.trust_of(tool)
        payload: Any = result.structured_content
        text = json.dumps(payload, default=str) if payload is not None else result_text(result)
        await self.provenance.record_tool(tool, trust, text)
        if trust != "untrusted" or not self.policy.taint.wrap_untrusted:
            return result
        wrapped = wrap_untrusted(tool, payload if payload is not None else text)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(wrapped))],
            structured_content=wrapped,
        )

    # --- approvals ------------------------------------------------------------------

    async def _park(
        self, tool: str, args: dict[str, Any], verdict: Verdict, event_id: int
    ) -> types.CallToolResult:
        """Store an escalated call as a pending approval. If that fails: DENY."""
        if self.approvals is None:
            return self.refusal(tool, verdict)
        try:
            approval = await self.approvals.create(
                session_id=self.session_id, tool=tool, args=args, rule_id=verdict.rule_id,
                reason=verdict.reason, audit_event_id=event_id,
                timeout_s=self.approval_timeout_s,
            )  # fmt: skip
        except Exception as exc:
            failed = Verdict("DENY", "approval_unavailable", f"could not queue approval: {exc}")
            return self.refusal(tool, failed)
        return self.refusal(
            tool, verdict, approval_id=approval.id, status="pending",
            expires_at=approval.expires_at.isoformat(),
        )  # fmt: skip

    async def resolve(self, approval_id: str) -> types.CallToolResult:
        """Operator only. Report an approval's state; if approved, run it exactly once.

        The stored arguments are executed, never new ones, after re-checking every deny
        rule (the market may have closed, or limits filled, while it waited).
        """
        with tracer.start_as_current_span(
            "gateway.resolve_approval", attributes={"tradeguard.approval_id": approval_id}
        ) as span:
            body = await self._resolve(approval_id)
            span.set_attribute("tradeguard.approval_status", str(body.get("status")))
        # JSON text only: resolve_approval is unlisted, so clients have no output schema to
        # validate structured content against (and the MCP client warns on every call).
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(body, default=str))]
        )

    async def _resolve(self, approval_id: str) -> dict[str, Any]:
        if self.approvals is None:
            return {"approval_id": approval_id, "status": "unknown", "executed": False}
        approval = await self.approvals.get(approval_id)
        if approval is None or approval.session_id != self.session_id:
            return {"approval_id": approval_id, "status": "unknown", "executed": False,
                    "reason": "no such approval in this session"}  # fmt: skip
        if approval.status == "approved":
            claimed = await self.approvals.claim(approval_id)
            if claimed is not None:
                return await self._execute_approved(claimed)
            approval = await self.approvals.get(approval_id) or approval
        return approval_view(approval)

    async def _execute_approved(self, approval: Approval) -> dict[str, Any]:
        if self.approvals is None:
            raise RuntimeError("approval store missing")
        call = ToolCall(approval.tool, approval.args)
        verdict = Verdict("ALLOW", "human_approved", "no policy configured")
        if self.pipeline is not None:
            verdict = await self.pipeline.decide(
                call, self.fetch_context, skip_approval=True, provenance=self.provenance
            )
        who = f"{approval.id} approved by {approval.decided_by}"
        if verdict.decision != "ALLOW":
            record = self._record(call, "DENY", verdict.rule_id,
                                  f"{who} but blocked at execution: {verdict.reason}")  # fmt: skip
            event_id = await self.audit.record_decision(record)
            await self.audit.record_outcome(event_id, AuditOutcome(0.0, 0.0, True, verdict.reason))
            body = {**approval_view(approval), "status": "blocked", "decision": "DENY",
                    "rule_id": verdict.rule_id, "reason": verdict.reason,
                    "executed": False}  # fmt: skip
            await self.approvals.finish(approval.id, "blocked", body)
            return body

        started = time.perf_counter()
        event_id = await self.audit.record_decision(
            self._record(call, "ALLOW", "human_approved", f"executing {who}: {approval.note or ''}")
        )
        result, upstream_ms = await self._forward(call.tool, call.args)
        text = result_text(result)
        await self.audit.record_outcome(event_id, AuditOutcome(
            (time.perf_counter() - started) * 1000, upstream_ms, bool(result.is_error),
            summarize_result(result.structured_content, text)))  # fmt: skip
        body = {**approval_view(approval), "status": "executed", "decision": "APPROVED",
                "executed": not result.is_error,
                "result": result.structured_content if not result.is_error else None,
                "error": text if result.is_error else None}  # fmt: skip
        await self.approvals.finish(approval.id, "executed", body)
        return body

    def _record(self, call: ToolCall, decision: str, rule_id: str, reason: str) -> DecisionRecord:
        trace_id, span_id = current_ids()
        upstream = self._routes[call.tool][0] if call.tool in self._routes else None
        return DecisionRecord(
            session_id=self.session_id, upstream=upstream, tool=call.tool, args=call.args,
            decision=decision,  # type: ignore[arg-type]
            rule_id=rule_id, reason=reason, trace_id=trace_id, span_id=span_id,
        )  # fmt: skip

    def refusal(self, tool: str, verdict: Verdict, **extra: Any) -> types.CallToolResult:
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
        body.update(extra)
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


def approval_view(approval: Approval) -> dict[str, Any]:
    """What resolve_approval reports for an approval that was not just executed."""
    if approval.status in ("executed", "blocked") and approval.result:
        return dict(approval.result)
    labels = {"pending": "ESCALATE", "approved": "APPROVED", "executing": "APPROVED",
              "rejected": "REJECTED", "expired": "EXPIRED"}  # fmt: skip
    decision = labels.get(approval.status, "DENY")
    return {
        "approval_id": approval.id,
        "status": approval.status,
        "decision": decision,
        "tool": approval.tool,
        "args": approval.args,
        "reason": approval.reason,
        "decided_by": approval.decided_by,
        "note": approval.note,
        "executed": False,
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
