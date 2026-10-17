"""Pipeline ordering and fail closed behavior."""

from pathlib import Path
from typing import Any

import pytest

from services.gateway.pipeline import rules
from services.gateway.pipeline.pipeline import Pipeline
from services.gateway.pipeline.types import PolicyContext, ToolCall
from services.gateway.policy.model import TaintConfig, load_policy

REPO_ROOT = Path(__file__).resolve().parents[2]
# These tests cover stages 1, 2, 3 and 6. Taint (stage 4) is tested in tests/provenance.
POLICY = load_policy(REPO_ROOT / "policies/default.yaml").model_copy(
    update={"taint": TaintConfig(enabled=False)}
)
PLACE_ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "side": {"type": "string"},
        "qty": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "notional": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "order_type": {"type": "string"},
        "limit_price": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    },
    "required": ["symbol", "side"],
}
SCHEMAS = {
    "place_order": PLACE_ORDER_SCHEMA,
    "get_quote": {"type": "object", "properties": {"symbol": {"type": "string"}},
                  "required": ["symbol"]},
    "transfer_cash": {"type": "object", "properties": {}},
    "cancel_order": {"type": "object", "properties": {"order_id": {"type": "string"}},
                     "required": ["order_id"]},
}  # fmt: skip


class Fetcher:
    def __init__(self, context: PolicyContext | None = None, error: bool = False) -> None:
        self.calls = 0
        self.context = context or PolicyContext(
            price=100, held_qty=0, positions_value=0, equity=10000, market_open=True,
            orders_today=0,
        )  # fmt: skip
        self.error = error

    async def __call__(self, call: ToolCall) -> PolicyContext:
        self.calls += 1
        if self.error:
            raise ConnectionError("upstream down")
        return self.context


async def decide(tool: str, args: dict[str, Any], fetcher: Fetcher | None = None) -> Any:
    pipeline = Pipeline(POLICY, SCHEMAS)
    return await pipeline.decide(ToolCall(tool, args), fetcher or Fetcher())


async def test_schema_violation_is_denied_first() -> None:
    verdict = await decide("place_order", {"symbol": 5, "side": "buy", "notional": 10})
    assert (verdict.decision, verdict.rule_id) == ("DENY", "schema_invalid")
    assert "symbol" in verdict.reason
    missing = await decide("place_order", {"side": "buy", "notional": 10})
    assert missing.rule_id == "schema_invalid"


async def test_tool_not_on_allowlist_is_denied() -> None:
    verdict = await decide("transfer_cash", {})
    assert (verdict.decision, verdict.rule_id) == ("DENY", "tool_not_allowed")


async def test_read_tool_is_allowed_without_fetching_context() -> None:
    fetcher = Fetcher()
    verdict = await decide("get_quote", {"symbol": "NFLX"}, fetcher)
    assert (verdict.decision, verdict.rule_id) == ("ALLOW", "policy_pass")
    assert fetcher.calls == 0


async def test_static_rules_run_before_context_fetch() -> None:
    fetcher = Fetcher()
    verdict = await decide("place_order", {"symbol": "ZZZZ", "side": "buy", "notional": 9e9},
                           fetcher)  # fmt: skip
    assert verdict.rule_id == "symbol_not_allowed"
    assert fetcher.calls == 0


async def test_deny_beats_escalate() -> None:
    verdict = await decide("place_order", {"symbol": "TSLA", "side": "buy", "notional": 3000})
    assert (verdict.decision, verdict.rule_id) == ("DENY", "max_order_notional")


async def test_escalate_when_only_approval_rule_fires() -> None:
    verdict = await decide("place_order", {"symbol": "NVDA", "side": "buy", "notional": 1500})
    assert (verdict.decision, verdict.rule_id) == ("ESCALATE", "approval_notional")


async def test_allowed_order() -> None:
    verdict = await decide("place_order", {"symbol": "AMD", "side": "buy", "notional": 500})
    assert (verdict.decision, verdict.rule_id) == ("ALLOW", "policy_pass")


async def test_context_failure_denies() -> None:
    verdict = await decide(
        "place_order", {"symbol": "AMD", "side": "buy", "notional": 500}, Fetcher(error=True)
    )
    assert (verdict.decision, verdict.rule_id) == ("DENY", "context_unavailable")


async def test_rule_crash_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: object, call: ToolCall, context: object) -> None:
        raise ZeroDivisionError("bad math")

    monkeypatch.setattr(rules.MaxPortfolioExposure, "evaluate", boom)
    verdict = await decide("place_order", {"symbol": "AMD", "side": "buy", "notional": 500})
    assert (verdict.decision, verdict.rule_id) == ("DENY", "pipeline_error")
    assert "ZeroDivisionError" in verdict.reason


async def test_cancel_order_is_allowed_by_allowlist() -> None:
    verdict = await decide("cancel_order", {"order_id": "ord_1"})
    assert verdict.decision == "ALLOW"


async def test_tool_without_known_schema_is_denied() -> None:
    verdict = await decide("list_orders", {})  # allowlisted, but no schema registered
    assert (verdict.decision, verdict.rule_id) == ("DENY", "schema_unknown")


async def test_taint_on_without_provenance_denies_writes() -> None:
    tainted = load_policy(REPO_ROOT / "policies/default.yaml")
    pipeline = Pipeline(tainted, SCHEMAS)
    order = ToolCall("place_order", {"symbol": "AMD", "side": "buy", "notional": 500})
    verdict = await pipeline.decide(order, Fetcher())
    assert (verdict.decision, verdict.rule_id) == ("DENY", "provenance_unavailable")
    read = await pipeline.decide(ToolCall("get_quote", {"symbol": "AMD"}), Fetcher())
    assert read.decision == "ALLOW"
