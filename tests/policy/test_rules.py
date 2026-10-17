"""Every rule: allow, deny and boundary values."""

from pathlib import Path
from typing import Any

import pytest

from services.gateway.pipeline.rules import (
    ApprovalNotional,
    ApprovalSellPct,
    MarketHours,
    MaxOrderNotional,
    MaxOrdersPerDay,
    MaxPortfolioExposure,
    MaxPositionNotional,
    NoShort,
    OrderShape,
    OrderTypeAllowed,
    Rule,
    SymbolAllowed,
)
from services.gateway.pipeline.types import PolicyContext, ToolCall
from services.gateway.policy.model import Policy, load_policy

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = load_policy(REPO_ROOT / "policies/default.yaml")


def order(**args: Any) -> ToolCall:
    base: dict[str, Any] = {"symbol": "NVDA", "side": "buy"}
    base.update(args)
    return ToolCall(tool="place_order", args=base)


def ctx(**overrides: Any) -> PolicyContext:
    values: dict[str, Any] = {
        "price": 100.0,
        "held_qty": 0.0,
        "positions_value": 0.0,
        "equity": 10000.0,
        "market_open": True,
        "orders_today": 0,
    }
    values.update(overrides)
    return PolicyContext(**values)


def decision(rule: Rule, call: ToolCall, context: PolicyContext | None = None) -> str:
    return rule.evaluate(call, context or ctx()).decision


def with_policy(**changes: Any) -> Policy:
    return POLICY.model_copy(update=changes)


# --- static rules -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"notional": 500}, "ALLOW"),
        ({"qty": 1}, "ALLOW"),
        ({"qty": 1, "notional": 500}, "DENY"),
        ({}, "DENY"),
        ({"qty": 0}, "DENY"),
        ({"notional": -5}, "DENY"),
        ({"side": "hold", "qty": 1}, "DENY"),
        ({"order_type": "limit", "qty": 1}, "DENY"),  # limit without limit_price
        ({"order_type": "limit", "qty": 1, "limit_price": 100}, "ALLOW"),
    ],
)
def test_order_shape(args: dict[str, Any], expected: str) -> None:
    assert decision(OrderShape(POLICY), order(**args)) == expected


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [("NVDA", "ALLOW"), ("nvda", "ALLOW"), (" QQQ ", "ALLOW"), ("NFLX", "DENY"), ("ZZZZ", "DENY")],
)
def test_symbol_allowed(symbol: str, expected: str) -> None:
    assert decision(SymbolAllowed(POLICY), order(symbol=symbol, qty=1)) == expected


@pytest.mark.parametrize(
    ("order_type", "expected"), [("market", "ALLOW"), ("limit", "ALLOW"), ("stop", "DENY")]
)
def test_order_type_allowed(order_type: str, expected: str) -> None:
    assert decision(OrderTypeAllowed(POLICY), order(qty=1, order_type=order_type)) == expected


def test_order_type_defaults_to_market() -> None:
    strict = with_policy(allowed_order_types=["limit"])
    assert decision(OrderTypeAllowed(strict), order(qty=1)) == "DENY"


# --- context rules ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"notional": 2000}, "ALLOW"),  # boundary: equal to the limit
        ({"notional": 2000.01}, "DENY"),
        ({"qty": 20}, "ALLOW"),  # 20 * 100
        ({"qty": 21}, "DENY"),
        ({"qty": 10, "order_type": "limit", "limit_price": 250}, "DENY"),  # uses limit price
        ({"side": "sell", "qty": 25}, "DENY"),  # sells are sized too
    ],
)
def test_max_order_notional(args: dict[str, Any], expected: str) -> None:
    assert decision(MaxOrderNotional(POLICY), order(**args)) == expected


@pytest.mark.parametrize(
    ("args", "held", "expected"),
    [
        ({"side": "sell", "qty": 10}, 10.0, "ALLOW"),  # boundary: sell everything
        ({"side": "sell", "qty": 10.5}, 10.0, "DENY"),
        ({"side": "sell", "qty": 1}, 0.0, "DENY"),
        ({"side": "sell", "notional": 1000}, 10.0, "ALLOW"),  # 10 shares at 100
        ({"side": "sell", "notional": 1001}, 10.0, "DENY"),
        ({"side": "buy", "qty": 5}, 0.0, "ALLOW"),
    ],
)
def test_no_short(args: dict[str, Any], held: float, expected: str) -> None:
    assert decision(NoShort(POLICY), order(**args), ctx(held_qty=held)) == expected


def test_no_short_off_when_policy_allows_shorts() -> None:
    rule = NoShort(with_policy(allow_short=True))
    assert decision(rule, order(side="sell", qty=5), ctx(held_qty=0)) == "ALLOW"


@pytest.mark.parametrize(
    ("notional", "expected"), [(2000, "ALLOW"), (2000.01, "DENY")]
)  # held 30 shares at 100 = 3000, limit 5000
def test_max_position_notional(notional: float, expected: str) -> None:
    context = ctx(held_qty=30, positions_value=3000)
    assert decision(MaxPositionNotional(POLICY), order(notional=notional), context) == expected


def test_max_position_ignores_sells() -> None:
    context = ctx(held_qty=60, positions_value=6000)
    assert decision(MaxPositionNotional(POLICY), order(side="sell", qty=1), context) == "ALLOW"


@pytest.mark.parametrize(
    ("notional", "expected"), [(1000, "ALLOW"), (1001, "DENY")]
)  # equity 10000, positions 5000: limit 60% = 6000
def test_max_portfolio_exposure(notional: float, expected: str) -> None:
    context = ctx(positions_value=5000, equity=10000)
    assert decision(MaxPortfolioExposure(POLICY), order(notional=notional), context) == expected


def test_exposure_with_no_equity_denies() -> None:
    context = ctx(positions_value=0, equity=0)
    assert decision(MaxPortfolioExposure(POLICY), order(notional=1), context) == "DENY"


@pytest.mark.parametrize(("today", "expected"), [(9, "ALLOW"), (10, "DENY"), (11, "DENY")])
def test_max_orders_per_day(today: int, expected: str) -> None:
    assert decision(MaxOrdersPerDay(POLICY), order(qty=1), ctx(orders_today=today)) == expected


@pytest.mark.parametrize(
    ("hours_only", "is_open", "expected"),
    [(True, True, "ALLOW"), (True, False, "DENY"), (False, False, "ALLOW")],
)
def test_market_hours(hours_only: bool, is_open: bool, expected: str) -> None:
    rule = MarketHours(with_policy(market_hours_only=hours_only))
    assert decision(rule, order(qty=1), ctx(market_open=is_open)) == expected


# --- approval rules ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [({"notional": 1000}, "ALLOW"), ({"notional": 1000.01}, "ESCALATE"), ({"qty": 11}, "ESCALATE")],
)
def test_approval_notional(args: dict[str, Any], expected: str) -> None:
    assert decision(ApprovalNotional(POLICY), order(**args)) == expected


@pytest.mark.parametrize(
    ("qty", "expected"), [(5, "ALLOW"), (6, "ESCALATE"), (10, "ESCALATE")]
)  # held 10, threshold 50%
def test_approval_sell_pct(qty: float, expected: str) -> None:
    context = ctx(held_qty=10)
    assert decision(ApprovalSellPct(POLICY), order(side="sell", qty=qty), context) == expected


def test_approval_sell_pct_ignores_buys() -> None:
    assert decision(ApprovalSellPct(POLICY), order(qty=100), ctx(held_qty=1)) == "ALLOW"


def test_every_result_has_rule_id_and_reason() -> None:
    rules: list[Rule] = [
        OrderShape(POLICY), SymbolAllowed(POLICY), OrderTypeAllowed(POLICY),
        MaxOrderNotional(POLICY), NoShort(POLICY), MaxPositionNotional(POLICY),
        MaxPortfolioExposure(POLICY), MaxOrdersPerDay(POLICY), MarketHours(POLICY),
        ApprovalNotional(POLICY), ApprovalSellPct(POLICY),
    ]  # fmt: skip
    ids = set()
    for rule in rules:
        for call in (order(notional=500), order(symbol="ZZZZ", side="sell", qty=99)):
            result = rule.evaluate(call, ctx(held_qty=1, market_open=False))
            assert result.rule_id == rule.rule_id
            assert result.reason
        ids.add(rule.rule_id)
    assert len(ids) == len(rules)
