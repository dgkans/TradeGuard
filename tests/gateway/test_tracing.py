"""Gateway spans nest under agent spans and carry the decision."""

from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from services.agent.graph import AgentContext, build_graph, run_turn
from services.agent.tools import McpToolset, ToolCallLog
from services.gateway.audit import MemoryAuditSink
from services.gateway.mcp_proxy.proxy import Gateway, Upstream
from services.replay_server.scenario import load_scenario
from services.replay_server.server import build_server
from services.replay_server.session import ReplaySession
from tests.agent.conftest import ScriptedChatModel, answer, plan, tool_call, understand

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORTER = InMemorySpanExporter()


def install_provider() -> None:
    """The global provider can be set once per process; reuse it across tests."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(EXPORTER))


def ancestors(span: ReadableSpan, by_id: dict[int, ReadableSpan]) -> list[str]:
    names = []
    parent = span.parent
    while parent is not None and parent.span_id in by_id:
        current = by_id[parent.span_id]
        names.append(current.name)
        parent = current.parent
    return names


async def test_gateway_decision_span_is_under_agent_tool_span() -> None:
    install_provider()
    EXPORTER.clear()
    session = ReplaySession(
        load_scenario("demo", REPO_ROOT / "scenarios"),
        bars_root=REPO_ROOT / "fixtures/bars",
        news_root=REPO_ROOT / "fixtures/news",
    )
    model = ScriptedChatModel(
        [understand("read_only", "quote"), plan(), tool_call("get_quote", {"symbol": "NVDA"}),
         answer("ok")]
    )  # fmt: skip
    audit = MemoryAuditSink()
    gateway = Gateway(
        [Upstream("replay", build_server(session))], audit=audit, session_id="trace-test"
    )
    async with gateway, McpToolset(gateway.server, log=ToolCallLog()) as toolset:
        graph = build_graph(model, model, toolset.tools, context=AgentContext("demo", ["NVDA"]))
        await run_turn(graph, "quote NVDA", thread_id="trace-test")

    spans = EXPORTER.get_finished_spans()
    by_id = {span.context.span_id: span for span in spans}
    decision = [s for s in spans if s.name == "gateway.decision"]
    assert len(decision) == 1
    attributes = dict(decision[0].attributes or {})
    assert attributes["tradeguard.decision"] == "ALLOW"
    assert attributes["tradeguard.tool"] == "get_quote"
    assert attributes["tradeguard.session_id"] == "trace-test"

    chain = ancestors(decision[0], by_id)
    assert "agent.tool_call" in chain
    assert "agent.turn" in chain
    assert chain.index("agent.tool_call") < chain.index("agent.turn")

    upstream = [s for s in spans if s.name == "gateway.upstream_call"]
    assert ancestors(upstream[0], by_id)[0] == "gateway.decision"
    # The audit row points at the same trace.
    assert audit.events[0]["trace_id"] == format(decision[0].context.trace_id, "032x")
