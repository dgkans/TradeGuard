"""Against the real Alpaca PAPER API, through the real gateway. Skipped without paper keys.

    TRADING_MODE=paper (and keys in .env)  uv run pytest -m alpaca -v

Places a 1 share SPY limit buy at $1 (it can never fill), lists it, cancels it, and checks
that a policy denial never reaches Alpaca. Market hours aren't required: the policy copy
here turns off only the market hours rule, because a DAY limit order is accepted (queued)
while the market is closed.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from mcp import Client

from services.alpaca_adapter.adapter import (
    PaperModeError,
    alpaca_server_params,
    connect_adapter,
    require_paper_settings,
)
from services.common.config import load_settings
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import MemoryAuditSink
from services.gateway.mcp_proxy.proxy import OPERATOR_META_KEY, Gateway, Upstream
from services.gateway.policy.model import load_policy
from services.gateway.provenance.store import MemoryProvenanceStore

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "op-live-paper"  # noqa: S105 - test value
OPERATOR = cast(Any, {OPERATOR_META_KEY: TOKEN})


def paper_configured() -> bool:
    try:
        require_paper_settings(load_settings())
    except (PaperModeError, Exception):
        return False
    return True


pytestmark = [
    pytest.mark.alpaca,
    pytest.mark.skipif(not paper_configured(), reason="needs TRADING_MODE=paper and keys"),
]


@asynccontextmanager
async def paper_gateway() -> AsyncIterator[tuple[Client, MemoryAuditSink]]:
    policy = load_policy(REPO_ROOT / "policies/default.yaml")
    policy = policy.model_copy(update={"market_hours_only": False})
    audit = MemoryAuditSink()
    params = alpaca_server_params(load_settings())
    async with connect_adapter(params) as adapter:
        gateway = Gateway(
            [Upstream("alpaca", adapter)], audit=audit, session_id="pytest-live-paper",
            policy=policy, operator_token=TOKEN, approvals=MemoryApprovalStore(audit=audit),
            provenance=MemoryProvenanceStore(),
        )  # fmt: skip
        async with gateway, Client(gateway.server) as client:
            yield client, audit


def body(result: Any) -> dict[str, Any]:
    if result.structured_content is not None:
        return cast(dict[str, Any], result.structured_content)
    data: dict[str, Any] = json.loads(result.content[0].text)
    return data


async def say(client: Client, text: str) -> None:
    result = await client.call_tool("record_user_message", {"text": text}, meta=OPERATOR)
    assert not result.is_error, result.content


async def test_place_list_and_cancel_a_real_paper_order() -> None:
    async with paper_gateway() as (client, audit):
        account = body(await client.call_tool("get_account", {}))
        assert account["mode"] == "paper" and account["account_id"].startswith("paper-****")

        await say(client, "Buy 1 share of SPY with a limit at $1")
        placed = await client.call_tool("place_order", {"symbol": "SPY", "side": "buy",
                                                        "qty": 1, "order_type": "limit",
                                                        "limit_price": 1})  # fmt: skip
        assert not placed.is_error, placed.content
        order = body(placed)
        try:
            assert order["status"] in ("new", "accepted", "pending_new")
            listed = body(await client.call_tool("list_orders", {"status": "open"}))
            assert order["id"] in [item["id"] for item in listed["orders"]]
        finally:
            await say(client, "Cancel my SPY order")
            canceled = await client.call_tool("cancel_order", {"order_id": order["id"]})
        assert not canceled.is_error, canceled.content
        assert body(canceled)["status"] in ("canceled", "pending_cancel")

        await say(client, "Buy $3000 of SPY")
        denied = await client.call_tool(
            "place_order", {"symbol": "SPY", "side": "buy", "notional": 3000}
        )
        assert denied.is_error and body(denied)["rule_id"] == "max_order_notional"

    decisions = [(e["tool"], e["decision"]) for e in audit.events if e["tool"] == "place_order"]
    assert decisions == [("place_order", "ALLOW"), ("place_order", "DENY")]
