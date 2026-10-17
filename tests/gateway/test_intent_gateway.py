"""Intent alignment end to end in the gateway on the demo scenario (NVDA ~$212.74, 10 held)."""

import json
from pathlib import Path
from typing import Any, cast

from mcp import Client

from services.agent.graph import AgentContext, build_graph, run_turn
from services.agent.provenance import report_user_message
from services.agent.tools import McpToolset, ToolCallLog
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import MemoryAuditSink
from services.gateway.mcp_proxy.proxy import OPERATOR_META_KEY, Gateway, Upstream
from services.gateway.policy.model import load_policy
from services.replay_server.scenario import load_scenario
from services.replay_server.server import build_server
from services.replay_server.session import ReplaySession
from tests.agent.conftest import ScriptedChatModel, answer, plan, tool_call, understand
from tests.intent.test_stage import FakeJudge

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "op-test-token"  # noqa: S105 - test value
OPERATOR = cast(Any, {OPERATOR_META_KEY: TOKEN})


class Rig:
    def __init__(self, judge: FakeJudge | None = None) -> None:
        self.session = ReplaySession(
            load_scenario("demo", REPO_ROOT / "scenarios"),
            bars_root=REPO_ROOT / "fixtures/bars",
            news_root=REPO_ROOT / "fixtures/news",
        )
        self.audit = MemoryAuditSink()
        self.judge = judge or FakeJudge()
        self.gateway = Gateway(
            [Upstream("replay", build_server(self.session, dev_tools=True))],
            audit=self.audit, session_id="intent",
            policy=load_policy(REPO_ROOT / "policies/default.yaml"), operator_token=TOKEN,
            approvals=MemoryApprovalStore(audit=self.audit), judge=self.judge,
        )  # fmt: skip

    def orders(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self.session.list_orders()["orders"]
        return result


async def order_after(rig: Rig, text: str, args: dict[str, Any]) -> dict[str, Any]:
    async with rig.gateway, Client(rig.gateway.server) as client:
        await client.call_tool("record_user_message", {"text": text}, meta=OPERATOR)
        result = await client.call_tool("place_order", args)
    if result.is_error:
        data: dict[str, Any] = json.loads(getattr(result.content[0], "text", "{}"))
        return data
    return {"decision": "ALLOW", **(result.structured_content or {})}


async def test_dollars_used_as_shares_is_escalated_not_executed() -> None:
    rig = Rig()
    body = await order_after(rig, "Buy $8 of NVDA", {"symbol": "NVDA", "side": "buy", "qty": 8})
    assert (body["decision"], body["rule_id"]) == ("ESCALATE", "intent_unit_confusion")
    assert "$8" in body["reason"] and "8 shares" in body["reason"]
    assert body["status"] == "pending"
    assert rig.orders() == []
    assert rig.judge.calls == 0


async def test_wrong_side_and_wrong_symbol_are_denied() -> None:
    rig = Rig()
    side = await order_after(rig, "Buy $500 of NVDA", {"symbol": "NVDA", "side": "sell", "qty": 2})
    assert (side["decision"], side["rule_id"]) == ("DENY", "intent_wrong_side")
    rig = Rig()
    symbol = await order_after(rig, "Buy $500 of AMD",
                               {"symbol": "NVDA", "side": "buy", "notional": 500})  # fmt: skip
    assert (symbol["decision"], symbol["rule_id"]) == ("DENY", "intent_wrong_symbol")
    assert rig.orders() == []


async def test_aligned_order_executes_and_audit_shows_intent() -> None:
    rig = Rig()
    body = await order_after(rig, "Buy $500 of AMD",
                             {"symbol": "AMD", "side": "buy", "notional": 500})  # fmt: skip
    assert body["decision"] == "ALLOW"
    assert len(rig.orders()) == 1
    reason = rig.audit.events[-1]["reason"]
    assert "intent: aligned" in reason and "judge: not needed" in reason


async def test_judge_runs_only_for_the_ambiguous_message() -> None:
    rig = Rig()
    async with rig.gateway, Client(rig.gateway.server) as client:
        for text, args in [
            ("Buy $500 of AMD", {"symbol": "AMD", "side": "buy", "notional": 500}),
            ("buy 1000 NVDA", {"symbol": "NVDA", "side": "buy", "notional": 1000}),
            ("Buy $300 of AMD", {"symbol": "AMD", "side": "buy", "notional": 300}),
        ]:
            await client.call_tool("record_user_message", {"text": text}, meta=OPERATOR)
            await client.call_tool("place_order", args)
    assert rig.judge.calls == 1
    rules = [e["rule_id"] for e in rig.audit.events if e["tool"] == "place_order"]
    assert rules == ["policy_pass", "intent_ambiguous_unit", "policy_pass"]


async def test_buy_1000_nvda_meant_as_dollars_is_escalated_with_explanation() -> None:
    """The plan's F8 demo, through the agent: the order waits for a human."""
    rig = Rig()
    model = ScriptedChatModel([
        understand("trade", "buy 1000 NVDA"), plan(),
        tool_call("place_order", {"symbol": "NVDA", "side": "buy", "notional": 1000}),
        answer("unused"),
    ])  # fmt: skip
    async with rig.gateway, McpToolset(rig.gateway.server, log=ToolCallLog()) as toolset:
        from langgraph.checkpoint.memory import InMemorySaver

        graph = build_graph(model, model, toolset.tools, context=AgentContext("demo", ["NVDA"]),
                            checkpointer=InMemorySaver())  # fmt: skip
        await report_user_message(toolset, "buy 1000 NVDA", OPERATOR)
        result = await run_turn(graph, "buy 1000 NVDA", thread_id="demo")
        pending = result["pending_approval"]
        assert pending["rule_id"] == "intent_ambiguous_unit"
        assert "1000 shares" in pending["reason"] and "$1000" in pending["reason"]
        assert rig.orders() == []
        status = await toolset.call("resolve_approval", {"approval_id": pending["approval_id"]},
                                    OPERATOR)  # fmt: skip
    assert status["status"] == "pending"
