"""Policy enforced end to end in the gateway on the demo scenario (10:30 ET, NVDA x10)."""

import json
from pathlib import Path
from typing import Any

from mcp import Client

from services.gateway.audit import MemoryAuditSink
from services.gateway.mcp_proxy.proxy import OPERATOR_META_KEY, Gateway, Upstream
from services.gateway.policy.model import load_policy
from services.replay_server.scenario import load_scenario
from services.replay_server.server import build_server
from services.replay_server.session import ReplaySession

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = load_policy(REPO_ROOT / "policies/default.yaml")
TOKEN = "op-test-token"  # noqa: S105 - test value, not a credential


def demo_session(
    start_time: str | None = None, positions: dict[str, float] | None = None
) -> ReplaySession:
    scenario = load_scenario("demo", REPO_ROOT / "scenarios")
    changes: dict[str, Any] = {}
    if start_time is not None:
        changes["start_time"] = start_time
    if positions is not None:
        changes["positions"] = positions
    scenario = scenario.model_validate({**scenario.model_dump(), **changes})
    return ReplaySession(
        scenario, bars_root=REPO_ROOT / "fixtures/bars", news_root=REPO_ROOT / "fixtures/news"
    )


def guarded(session: ReplaySession, audit: MemoryAuditSink) -> Gateway:
    return Gateway(
        [Upstream("replay", build_server(session, dev_tools=True))],
        audit=audit,
        session_id="policy-test",
        policy=POLICY,
        operator_token=TOKEN,
    )


async def call(gateway: Gateway, tool: str, args: dict[str, Any], **kw: Any) -> Any:
    async with Client(gateway.server) as client:
        return await client.call_tool(tool, args, **kw)


def refusal(result: Any) -> dict[str, Any]:
    assert result.is_error
    data: dict[str, Any] = json.loads(result.content[0].text)
    return data


async def test_oversize_order_denied_with_structured_reason() -> None:
    session, audit = demo_session(), MemoryAuditSink()
    async with guarded(session, audit) as gateway:
        result = await call(gateway, "place_order",
                            {"symbol": "TSLA", "side": "buy", "notional": 3000})  # fmt: skip
    body = refusal(result)
    assert body["decision"] == "DENY"
    assert body["rule_id"] == "max_order_notional"
    assert "2000" in body["reason"]
    assert body["policy"] == "default"
    assert body["executed"] is False
    assert session.list_orders()["orders"] == []


async def test_order_over_approval_threshold_escalates_and_is_not_placed() -> None:
    session, audit = demo_session(), MemoryAuditSink()
    async with guarded(session, audit) as gateway:
        result = await call(gateway, "place_order",
                            {"symbol": "NVDA", "side": "buy", "notional": 1500})  # fmt: skip
    body = refusal(result)
    assert (body["decision"], body["rule_id"]) == ("ESCALATE", "approval_notional")
    assert session.list_orders()["orders"] == []
    assert audit.events[0]["decision"] == "ESCALATE"


async def test_allowed_order_is_forwarded() -> None:
    session, audit = demo_session(), MemoryAuditSink()
    async with guarded(session, audit) as gateway:
        result = await call(gateway, "place_order",
                            {"symbol": "AMD", "side": "buy", "notional": 500})  # fmt: skip
    assert not result.is_error
    assert result.structured_content["status"] == "new"
    assert audit.events[0]["rule_id"] == "policy_pass"


async def test_unknown_symbol_short_and_after_hours_are_denied() -> None:
    cases = [
        ({"symbol": "NFLX", "side": "buy", "notional": 100}, None, "symbol_not_allowed"),
        ({"symbol": "TSLA", "side": "sell", "qty": 1}, None, "no_short"),
        ({"symbol": "AMD", "side": "buy", "notional": 100}, "2026-09-15T21:00:00Z",
         "market_hours"),
    ]  # fmt: skip
    for args, start, expected in cases:
        session, audit = demo_session(start), MemoryAuditSink()
        async with guarded(session, audit) as gateway:
            body = refusal(await call(gateway, "place_order", args))
        assert (body["decision"], body["rule_id"]) == ("DENY", expected), args
        assert session.list_orders()["orders"] == []


async def test_selling_most_of_a_position_escalates() -> None:
    # 3 of 4 shares is 75% of the position but only about $638, under the $1000 threshold,
    # so the sell percentage rule is the one that fires.
    session, audit = demo_session(positions={"NVDA": 4}), MemoryAuditSink()
    async with guarded(session, audit) as gateway:
        body = refusal(await call(gateway, "place_order",
                                  {"symbol": "NVDA", "side": "sell", "qty": 3}))  # fmt: skip
    assert (body["decision"], body["rule_id"]) == ("ESCALATE", "approval_sell_pct")


async def test_dev_tool_denied_to_agent_but_allowed_to_operator() -> None:
    session, audit = demo_session(), MemoryAuditSink()
    async with guarded(session, audit) as gateway:
        agent = await call(gateway, "advance_clock", {"minutes": 5})
        wrong = await call(gateway, "advance_clock", {"minutes": 5},
                           meta={OPERATOR_META_KEY: "guess"})  # fmt: skip
        operator = await call(gateway, "advance_clock", {"minutes": 5},
                              meta={OPERATOR_META_KEY: TOKEN})  # fmt: skip
    assert refusal(agent)["rule_id"] == "tool_not_allowed"
    assert refusal(wrong)["rule_id"] == "tool_not_allowed"
    assert not operator.is_error
    assert operator.structured_content["now"] == "2026-09-15T14:35:00+00:00"
    assert [e["rule_id"] for e in audit.events] == ["tool_not_allowed", "tool_not_allowed",
                                                    "operator"]  # fmt: skip


async def test_operator_token_does_not_bypass_policy_for_orders() -> None:
    session, audit = demo_session(), MemoryAuditSink()
    async with guarded(session, audit) as gateway:
        result = await call(gateway, "place_order",
                            {"symbol": "TSLA", "side": "buy", "notional": 3000},
                            meta={OPERATOR_META_KEY: TOKEN})  # fmt: skip
    assert refusal(result)["rule_id"] == "max_order_notional"


async def test_every_audited_decision_has_rule_id_and_reason() -> None:
    session, audit = demo_session(), MemoryAuditSink()
    calls: list[tuple[str, dict[str, Any]]] = [
        ("get_quote", {"symbol": "NVDA"}),
        ("place_order", {"symbol": "AMD", "side": "buy", "notional": 500}),
        ("place_order", {"symbol": "TSLA", "side": "buy", "notional": 3000}),
        ("place_order", {"symbol": "NVDA", "side": "buy", "notional": 1500}),
        ("place_order", {"symbol": 7, "side": "buy"}),
        ("advance_clock", {"minutes": 1}),
        ("no_such_tool", {}),
    ]
    async with guarded(session, audit) as gateway, Client(gateway.server) as client:
        for tool, args in calls:
            await client.call_tool(tool, args)
    assert len(audit.events) == len(calls)
    for event in audit.events:
        assert event["rule_id"]
        assert event["reason"]
    decisions = [e["decision"] for e in audit.events]
    assert decisions == ["ALLOW", "ALLOW", "DENY", "ESCALATE", "DENY", "DENY", "DENY"]


async def test_context_reads_are_not_audited_as_agent_calls() -> None:
    session, audit = demo_session(), MemoryAuditSink()
    async with guarded(session, audit) as gateway:
        await call(gateway, "place_order", {"symbol": "AMD", "side": "buy", "notional": 500})
    assert [e["tool"] for e in audit.events] == ["place_order"]
