"""Pass-through gateway: same behavior as direct, every call audited, fail closed."""

import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from services.agent.graph import AgentContext, build_graph, run_turn
from services.agent.tools import McpToolset, ToolCallLog
from services.gateway.audit import AuditOutcome, DecisionRecord, MemoryAuditSink
from services.gateway.mcp_proxy.proxy import Gateway, GatewayConfigError, Upstream
from services.replay_server.scenario import load_scenario
from services.replay_server.server import build_server
from services.replay_server.session import ReplaySession
from tests.agent.conftest import ScriptedChatModel, answer, plan, tool_call, understand

REPO_ROOT = Path(__file__).resolve().parents[2]

SCRIPT: list[tuple[str, dict[str, Any]]] = [
    ("get_clock", {}),
    ("get_quote", {"symbol": "NVDA"}),
    ("get_bars", {"symbol": "AMD", "interval": "5m", "limit": 5}),
    ("get_news", {"limit": 5}),
    ("scan_watchlist", {"symbols": ["NVDA", "AMD", "AAPL"]}),
    ("place_order", {"symbol": "AMD", "side": "buy", "notional": 500}),
    ("place_order", {"symbol": "AAPL", "side": "buy", "notional": 99999}),
    ("advance_clock", {"minutes": 10}),
    ("list_orders", {}),
    ("get_positions", {}),
    ("get_account", {}),
    ("get_quote", {"symbol": "ZZZZ"}),
]


def demo_session() -> ReplaySession:
    return ReplaySession(
        load_scenario("demo", REPO_ROOT / "scenarios"),
        bars_root=REPO_ROOT / "fixtures/bars",
        news_root=REPO_ROOT / "fixtures/news",
    )


def gateway_for(session: ReplaySession, audit: Any, session_id: str = "s1") -> Gateway:
    upstream = Upstream(name="replay", target=build_server(session, dev_tools=True))
    return Gateway([upstream], audit=audit, session_id=session_id)


async def run_script(client: Client) -> list[Any]:
    out = []
    for name, args in SCRIPT:
        result = await client.call_tool(name, args)
        out.append((result.is_error, result.structured_content))
    return out


async def test_lists_union_of_upstream_tools() -> None:
    session = demo_session()
    async with Client(build_server(session, dev_tools=True)) as direct:
        expected = {tool.name for tool in (await direct.list_tools()).tools}
    async with gateway_for(session, MemoryAuditSink()) as gateway, Client(gateway.server) as c:
        listed = (await c.list_tools()).tools
    # Plus the gateway's own operator tool (no approval store here, so no resolve_approval).
    assert {tool.name for tool in listed} == expected | {"record_user_message"}
    schemas = {tool.name: tool.input_schema for tool in listed}
    assert "notional" in schemas["place_order"]["properties"]


async def test_results_identical_direct_and_through_gateway() -> None:
    direct_session = demo_session()
    async with Client(build_server(direct_session, dev_tools=True)) as direct:
        direct_results = await run_script(direct)

    proxied_session = demo_session()
    gateway = gateway_for(proxied_session, MemoryAuditSink())
    async with gateway, Client(gateway.server) as proxied:
        proxied_results = await run_script(proxied)

    assert proxied_results == direct_results
    assert proxied_session.list_orders() == direct_session.list_orders()


async def test_agent_places_same_orders_through_gateway() -> None:
    def script() -> ScriptedChatModel:
        return ScriptedChatModel(
            [
                understand("trade", "buy $500 of AMD"),
                plan(),
                tool_call("scan_watchlist", {"symbols": ["NVDA", "AMD"]}, call_id="a"),
                tool_call("place_order", {"symbol": "AMD", "side": "buy", "notional": 500}, "b"),
                answer("Bought AMD."),
            ]
        )

    context = AgentContext(scenario_id="demo", watchlist=["NVDA", "AMD"])
    direct_session = demo_session()
    direct_log = ToolCallLog()
    async with McpToolset(build_server(direct_session), log=direct_log) as ts:
        graph = build_graph(script(), script(), ts.tools, context=context)
        await run_turn(graph, "Buy $500 of AMD", thread_id="d")

    proxied_session = demo_session()
    proxied_log = ToolCallLog()
    gateway = gateway_for(proxied_session, MemoryAuditSink())
    async with gateway, McpToolset(gateway.server, log=proxied_log) as ts:
        graph = build_graph(script(), script(), ts.tools, context=context)
        await run_turn(graph, "Buy $500 of AMD", thread_id="g")

    assert proxied_session.list_orders() == direct_session.list_orders()
    direct_calls = [(r["tool"], r["args"], r["result"]) for r in direct_log.records]
    proxied_calls = [(r["tool"], r["args"], r["result"]) for r in proxied_log.records]
    assert proxied_calls == direct_calls


async def test_every_call_is_audited() -> None:
    audit = MemoryAuditSink()
    gateway = gateway_for(demo_session(), audit, session_id="audit-test")
    async with gateway, Client(gateway.server) as client:
        await run_script(client)

    assert len(audit.events) == len(SCRIPT)
    for event, (name, args) in zip(audit.events, SCRIPT, strict=True):
        assert event["session_id"] == "audit-test"
        assert event["upstream"] == "replay"
        assert event["tool"] == name
        assert event["args"] == args
        assert event["decision"] == "ALLOW"
        assert event["rule_id"] == "pass_through"
        assert event["latency_ms"] >= event["upstream_ms"] >= 0
        assert event["overhead_ms"] >= 0
        assert event["result_summary"]
    by_tool = {e["tool"]: e for e in audit.events}
    assert by_tool["get_quote"]["is_error"] is False or audit.events[-1]["is_error"] is True
    assert audit.events[-1]["is_error"] is True  # ZZZZ
    assert "unknown symbol" in audit.events[-1]["result_summary"]


class FailingAuditSink(MemoryAuditSink):
    async def record_decision(self, record: DecisionRecord) -> int:
        raise ConnectionError("database is down")

    async def record_outcome(self, event_id: int, outcome: AuditOutcome) -> None:
        raise AssertionError("must not be reached")


async def test_audit_failure_denies_and_never_forwards() -> None:
    session = demo_session()
    gateway = gateway_for(session, FailingAuditSink())
    async with gateway, Client(gateway.server) as client:
        result = await client.call_tool(
            "place_order", {"symbol": "AMD", "side": "buy", "notional": 500}
        )
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "DENY" in text and "audit" in text
    assert session.list_orders()["orders"] == []


async def test_unknown_tool_is_denied_and_audited() -> None:
    audit = MemoryAuditSink()
    gateway = gateway_for(demo_session(), audit)
    async with gateway, Client(gateway.server) as client:
        result = await client.call_tool("withdraw_funds", {"amount": 1})
    assert result.is_error
    assert audit.events[0]["decision"] == "DENY"
    assert audit.events[0]["rule_id"] == "unknown_tool"


async def test_tool_name_collision_refuses_to_start() -> None:
    upstreams = [
        Upstream(name="replay_a", target=build_server(demo_session())),
        Upstream(name="replay_b", target=build_server(demo_session())),
    ]
    with pytest.raises(GatewayConfigError, match=r"exposed by both 'replay_a' and 'replay_b'"):
        async with Gateway(upstreams, audit=MemoryAuditSink(), session_id="x"):
            pass


async def test_overhead_stats_report_p50_and_p95() -> None:
    gateway = gateway_for(demo_session(), MemoryAuditSink())
    async with gateway, Client(gateway.server) as client:
        for _ in range(20):
            await client.call_tool("get_clock", {})
        stats = gateway.overhead_stats()
    assert stats["calls"] == 20
    assert 0 <= stats["overhead_p50_ms"] <= stats["overhead_p95_ms"]
    assert stats["upstream_p50_ms"] >= 0


async def test_gateway_over_stdio_lists_replay_tools() -> None:
    session_id = f"stdio-{uuid.uuid4().hex[:6]}"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "services.gateway", "--session", session_id, "--scenario", "demo",
              "--audit", "memory", "--dev-tools"],
        cwd=REPO_ROOT,
    )  # fmt: skip
    async with Client(params) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"place_order", "scan_watchlist", "advance_clock"} <= names
        quote = await client.call_tool("get_quote", {"symbol": "NVDA"})
        assert not quote.is_error
        assert json.loads(json.dumps(quote.structured_content))["symbol"] == "NVDA"
