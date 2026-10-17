"""Agent pauses with a LangGraph interrupt on ESCALATE and resumes on the human decision."""

import asyncio
import json
from pathlib import Path
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from services.agent.approvals import wait_for_decision
from services.agent.graph import AgentContext, build_graph, resume_turn, run_turn
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

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "op-test-token"  # noqa: S105 - test value
OPERATOR = {OPERATOR_META_KEY: TOKEN}


def script(final: str) -> ScriptedChatModel:
    return ScriptedChatModel(
        [
            understand("trade", "buy $1500 of NVDA"),
            plan(),
            tool_call("place_order", {"symbol": "NVDA", "side": "buy", "notional": 1500}),
            answer(final),
        ]
    )


class Rig:
    def __init__(self, timeout_s: float = 60) -> None:
        self.session = ReplaySession(
            load_scenario("demo", REPO_ROOT / "scenarios"),
            bars_root=REPO_ROOT / "fixtures/bars",
            news_root=REPO_ROOT / "fixtures/news",
        )
        self.audit = MemoryAuditSink()
        self.approvals = MemoryApprovalStore(audit=self.audit)
        self.gateway = Gateway(
            [Upstream("replay", build_server(self.session, dev_tools=True))],
            audit=self.audit, session_id="flow",
            policy=load_policy(REPO_ROOT / "policies/default.yaml"), operator_token=TOKEN,
            approvals=self.approvals, approval_timeout_s=timeout_s,
        )  # fmt: skip

    def orders(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self.session.list_orders()["orders"]
        return result


def final_tool_result(model: ScriptedChatModel) -> dict[str, Any]:
    tool_messages = [m for m in model.prompts[-1] if isinstance(m, ToolMessage)]
    data: dict[str, Any] = json.loads(str(tool_messages[-1].content).removeprefix("ERROR: "))
    return data


async def run_until_decision(rig: Rig, model: ScriptedChatModel, decide: Any) -> dict[str, Any]:
    """Run a turn, check it paused, apply the human decision, wait, resume."""
    context = AgentContext("demo", ["NVDA"])
    async with rig.gateway, McpToolset(rig.gateway.server, log=ToolCallLog()) as toolset:
        graph = build_graph(model, model, toolset.tools, context=context,
                            checkpointer=InMemorySaver())  # fmt: skip
        await report_user_message(toolset, "Buy $1500 of NVDA", OPERATOR)
        first = await run_turn(graph, "Buy $1500 of NVDA", thread_id="t")
        assert first["answer"] is None
        pending = first["pending_approval"]
        assert pending["approval_id"].startswith("apr_")
        assert pending["tool"] == "place_order"
        assert rig.orders() == []  # not executed while waiting

        await decide(pending["approval_id"])
        outcome = await wait_for_decision(toolset, pending["approval_id"], OPERATOR,
                                          poll_seconds=0.01, max_wait_seconds=5)  # fmt: skip
        result = await resume_turn(graph, outcome, thread_id="t")
    result["outcome"] = outcome
    return result


async def test_order_executes_only_after_approval() -> None:
    rig = Rig()
    model = script("Approved and placed.")

    async def approve(approval_id: str) -> None:
        assert rig.orders() == []
        await rig.approvals.decide(approval_id, approve=True, decided_by="alice", note="ok")

    result = await run_until_decision(rig, model, approve)
    assert result["answer"] == "Approved and placed."
    assert result["outcome"]["status"] == "executed"
    orders = rig.orders()
    assert len(orders) == 1 and orders[0]["notional"] == 1500.0
    # The model saw the real execution result in place of the escalation.
    seen = final_tool_result(model)
    assert seen["decision"] == "APPROVED" and seen["executed"] is True


async def test_rejected_order_never_executes_and_agent_sees_why() -> None:
    rig = Rig()
    model = script("Your reviewer rejected it: too big for today.")

    async def reject(approval_id: str) -> None:
        await rig.approvals.decide(approval_id, approve=False, decided_by="bob",
                                   note="too big for today")  # fmt: skip

    result = await run_until_decision(rig, model, reject)
    assert rig.orders() == []
    seen = final_tool_result(model)
    assert seen["decision"] == "REJECTED"
    assert seen["note"] == "too big for today"
    assert seen["executed"] is False
    assert "too big for today" in result["answer"]


async def test_timeout_auto_rejects_and_never_executes() -> None:
    rig = Rig(timeout_s=0.05)
    model = script("The approval timed out, so nothing was placed.")

    async def wait_out(approval_id: str) -> None:
        await asyncio.sleep(0.1)

    result = await run_until_decision(rig, model, wait_out)
    assert result["outcome"]["status"] == "expired"
    assert final_tool_result(model)["decision"] == "EXPIRED"
    assert rig.orders() == []
    assert [e["rule_id"] for e in rig.audit.events][-1] == "approval_timeout"
