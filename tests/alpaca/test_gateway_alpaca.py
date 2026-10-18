"""The gateway in front of the Alpaca adapter makes the same decisions as in front of replay."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from mcp import Client

from services.alpaca_adapter.adapter import connect_adapter
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import MemoryAuditSink
from services.gateway.mcp_proxy.proxy import OPERATOR_META_KEY, Gateway, Upstream
from services.gateway.policy.model import load_policy
from services.gateway.provenance.store import MemoryProvenanceStore
from tests.alpaca.fake_alpaca import FakeAlpaca, build_fake_server

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "op-alpaca-test"  # noqa: S105 - test value
OPERATOR = cast(Any, {OPERATOR_META_KEY: TOKEN})


@asynccontextmanager
async def guarded(state: FakeAlpaca) -> AsyncIterator[tuple[Client, MemoryApprovalStore]]:
    audit = MemoryAuditSink()
    approvals = MemoryApprovalStore(audit=audit)
    async with connect_adapter(build_fake_server(state)) as adapter:
        gateway = Gateway(
            [Upstream("alpaca", adapter)], audit=audit, session_id="paper-test",
            policy=load_policy(REPO_ROOT / "policies/default.yaml"), operator_token=TOKEN,
            approvals=approvals, approval_timeout_s=60, provenance=MemoryProvenanceStore(),
        )  # fmt: skip
        async with gateway, Client(gateway.server) as client:
            yield client, approvals


async def say(client: Client, text: str) -> None:
    result = await client.call_tool("record_user_message", {"text": text}, meta=OPERATOR)
    assert not result.is_error, result.content


def body(result: Any) -> dict[str, Any]:
    if result.structured_content is not None:
        return cast(dict[str, Any], result.structured_content)
    data: dict[str, Any] = json.loads(result.content[0].text)
    return data


def sent_orders(state: FakeAlpaca) -> list[dict[str, Any]]:
    return [args for tool, args in state.calls if tool == "place_stock_order"]


async def test_place_list_and_cancel_a_paper_order_through_the_gateway() -> None:
    state = FakeAlpaca()
    async with guarded(state) as (client, _):
        await say(client, "Buy 1 share of NVDA with a limit at $1")
        placed = await client.call_tool(
            "place_order",
            {"symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "limit", "limit_price": 1},
        )
        assert not placed.is_error, placed.content
        order_id = body(placed)["id"]
        listed = body(await client.call_tool("list_orders", {"status": "open"}))
        await say(client, "Cancel my NVDA order")
        canceled = await client.call_tool("cancel_order", {"order_id": order_id})
    assert [o["id"] for o in listed["orders"]] == [order_id]
    assert not canceled.is_error, canceled.content
    assert body(canceled)["status"] == "canceled"
    assert len(sent_orders(state)) == 1


async def test_order_over_the_size_limit_is_denied_and_never_sent() -> None:
    state = FakeAlpaca()
    async with guarded(state) as (client, _):
        await say(client, "Buy $3000 of AMD")
        result = await client.call_tool(
            "place_order", {"symbol": "AMD", "side": "buy", "notional": 3000}
        )
    assert result.is_error
    assert body(result)["rule_id"] == "max_order_notional"  # same rule as in replay
    assert sent_orders(state) == []


async def test_order_over_the_approval_threshold_waits_then_executes_once() -> None:
    state = FakeAlpaca()
    async with guarded(state) as (client, approvals):
        await say(client, "Buy $1500 of NVDA")
        held = await client.call_tool(
            "place_order", {"symbol": "NVDA", "side": "buy", "notional": 1500}
        )
        parked = body(held)
        assert parked["decision"] == "ESCALATE" and parked["rule_id"] == "approval_notional"
        assert sent_orders(state) == []  # nothing reaches Alpaca while it waits
        await approvals.decide(parked["approval_id"], approve=True, decided_by="t", note="ok")
        first = body(
            await client.call_tool(
                "resolve_approval", {"approval_id": parked["approval_id"]}, meta=OPERATOR
            )
        )
        again = body(
            await client.call_tool(
                "resolve_approval", {"approval_id": parked["approval_id"]}, meta=OPERATOR
            )
        )
    assert first["status"] == "executed" and again["status"] == "executed"
    assert len(sent_orders(state)) == 1  # exactly once
    assert sent_orders(state)[0]["notional"] == "1500"


async def test_closed_market_is_denied_like_in_replay() -> None:
    state = FakeAlpaca(is_open=False)
    async with guarded(state) as (client, _):
        await say(client, "Buy $300 of AMD")
        result = await client.call_tool(
            "place_order", {"symbol": "AMD", "side": "buy", "notional": 300}
        )
    assert result.is_error
    assert body(result)["rule_id"] == "market_hours"
    assert sent_orders(state) == []


async def test_injected_order_after_reading_news_is_denied() -> None:
    state = FakeAlpaca()
    async with guarded(state) as (client, _):
        await say(client, "What's the news on NVDA?")
        news = await client.call_tool("get_news", {"symbol": "NVDA"})
        text = getattr(news.content[0], "text", "")
        assert "UNTRUSTED_DATA" in text  # real news is labelled untrusted too
        result = await client.call_tool("place_order", {"symbol": "NVDA", "side": "sell", "qty": 5})
    assert result.is_error
    assert body(result)["rule_id"] == "taint_unsupported"
    assert sent_orders(state) == []
