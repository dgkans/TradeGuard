"""Gateway: ESCALATE parks the call; resolve_approval executes only approved calls, once."""

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from mcp import Client

from services.agent.tools import McpToolset, ToolCallLog
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import MemoryAuditSink
from services.gateway.mcp_proxy.proxy import OPERATOR_META_KEY, Gateway, Upstream
from services.gateway.policy.model import load_policy
from services.replay_server.scenario import load_scenario
from services.replay_server.server import build_server
from services.replay_server.session import ReplaySession

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = load_policy(REPO_ROOT / "policies/default.yaml")
TOKEN = "op-test-token"  # noqa: S105 - test value
OPERATOR = {OPERATOR_META_KEY: TOKEN}
ESCALATING = {"symbol": "NVDA", "side": "buy", "notional": 1500}


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
            audit=self.audit,
            session_id="apr-test",
            policy=POLICY,
            operator_token=TOKEN,
            approvals=self.approvals,
            approval_timeout_s=timeout_s,
        )

    def orders(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self.session.list_orders()["orders"]
        return result


def body(result: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(result.content[0].text)
    return data


async def test_resolve_replies_with_json_text_only() -> None:
    """Regression: structured output on an unlisted tool made the MCP client log a
    warning on every poll."""
    rig = Rig()
    async with rig.gateway, Client(rig.gateway.server) as client:
        approval_id = await escalate(client)
        result = await client.call_tool(
            "resolve_approval", {"approval_id": approval_id}, meta=cast(Any, OPERATOR)
        )
    assert result.structured_content is None
    assert body(result)["status"] == "pending"


async def escalate(client: Client) -> str:
    result = await client.call_tool("place_order", ESCALATING)
    parked = body(result)
    assert parked["decision"] == "ESCALATE"
    assert parked["status"] == "pending"
    assert parked["executed"] is False
    approval_id: str = parked["approval_id"]
    return approval_id


async def resolve(client: Client, approval_id: str) -> dict[str, Any]:
    result = await client.call_tool(
        "resolve_approval", {"approval_id": approval_id}, meta=cast(Any, OPERATOR)
    )
    return body(result)


async def test_escalated_order_waits_then_executes_once_when_approved() -> None:
    rig = Rig()
    async with rig.gateway, Client(rig.gateway.server) as client:
        approval_id = await escalate(client)
        assert rig.orders() == []
        assert (await resolve(client, approval_id))["status"] == "pending"
        assert rig.orders() == []

        await rig.approvals.decide(approval_id, approve=True, decided_by="alice", note="ok")
        outcome = await resolve(client, approval_id)
        assert outcome["status"] == "executed"
        assert outcome["decision"] == "APPROVED"
        assert outcome["executed"] is True
        assert outcome["result"]["notional"] == 1500.0
        again = await resolve(client, approval_id)
        assert again["status"] == "executed"
    orders = rig.orders()
    assert len(orders) == 1
    assert (orders[0]["symbol"], orders[0]["notional"]) == ("NVDA", 1500.0)
    rules = [e["rule_id"] for e in rig.audit.events]
    assert rules == ["approval_notional", "human_approval", "human_approved"]


async def test_rejected_order_is_never_executed() -> None:
    rig = Rig()
    async with rig.gateway, Client(rig.gateway.server) as client:
        approval_id = await escalate(client)
        await rig.approvals.decide(approval_id, approve=False, decided_by="bob", note="too big")
        outcome = await resolve(client, approval_id)
    assert outcome["status"] == "rejected"
    assert outcome["decision"] == "REJECTED"
    assert outcome["note"] == "too big"
    assert outcome["executed"] is False
    assert rig.orders() == []


async def test_expired_approval_is_never_executed() -> None:
    rig = Rig(timeout_s=0.05)
    async with rig.gateway, Client(rig.gateway.server) as client:
        approval_id = await escalate(client)
        await asyncio.sleep(0.1)
        outcome = await resolve(client, approval_id)
    assert (outcome["status"], outcome["decision"]) == ("expired", "EXPIRED")
    assert rig.orders() == []


async def test_approved_order_is_rechecked_against_deny_rules() -> None:
    rig = Rig()
    async with rig.gateway, Client(rig.gateway.server) as client:
        approval_id = await escalate(client)
        await rig.approvals.decide(approval_id, approve=True, decided_by="alice", note="")
        # The market closes while the order waits.
        rig.session.advance(minutes=60 * 6)
        outcome = await resolve(client, approval_id)
    assert outcome["status"] == "blocked"
    assert outcome["rule_id"] == "market_hours"
    assert outcome["executed"] is False
    assert rig.orders() == []


async def test_resolve_needs_operator_token_and_is_hidden_from_the_agent() -> None:
    rig = Rig()
    async with rig.gateway, Client(rig.gateway.server) as client:
        approval_id = await escalate(client)
        await rig.approvals.decide(approval_id, approve=True, decided_by="alice", note="")
        agent_try = await client.call_tool("resolve_approval", {"approval_id": approval_id})
        wrong = await client.call_tool("resolve_approval", {"approval_id": approval_id},
                                       meta=cast(Any, {OPERATOR_META_KEY: "guess"}))  # fmt: skip
    assert agent_try.is_error and body(agent_try)["rule_id"] == "operator_only"
    assert wrong.is_error and body(wrong)["rule_id"] == "operator_only"
    assert rig.orders() == []  # an approved call still needs the operator to run it
    async with McpToolset(rig.gateway.server, log=ToolCallLog()) as toolset:
        assert "resolve_approval" not in {tool.name for tool in toolset.tools}


async def test_approval_store_failure_denies() -> None:
    class Broken(MemoryApprovalStore):
        async def create(self, **kwargs: Any) -> Any:
            raise ConnectionError("db down")

    rig = Rig()
    rig.gateway.approvals = Broken(audit=rig.audit)
    async with rig.gateway, Client(rig.gateway.server) as client:
        result = await client.call_tool("place_order", ESCALATING)
    assert body(result)["decision"] == "DENY"
    assert body(result)["rule_id"] == "approval_unavailable"
    assert rig.orders() == []
