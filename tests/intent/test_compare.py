"""Order vs parsed intent."""

from typing import Any

import pytest

from services.gateway.intent.compare import compare
from services.gateway.intent.parser import parse_intent
from services.gateway.pipeline.types import PolicyContext, ToolCall


def ctx(price: float = 100.0, held: float = 10.0) -> PolicyContext:
    return PolicyContext(price=price, held_qty=held, positions_value=held * price,
                         equity=20000, market_open=True, orders_today=0)  # fmt: skip


def order(side: str = "buy", symbol: str = "NVDA", **amount: Any) -> ToolCall:
    return ToolCall("place_order", {"symbol": symbol, "side": side, **amount})


def check(text: str, call: ToolCall, context: PolicyContext | None = None) -> tuple[str, str]:
    result = compare(parse_intent(text), call, context or ctx(), tolerance_pct=10)
    assert result.reason
    return result.decision, result.rule_id


@pytest.mark.parametrize(
    ("text", "call", "expected"),
    [
        ("Buy $500 of NVDA", order(notional=500), ("ALLOW", "intent_aligned")),
        ("Buy $500 of NVDA", order(qty=5), ("ALLOW", "intent_aligned")),  # 5 x 100
        ("Buy $500 of NVDA", order(notional=540), ("ALLOW", "intent_aligned")),  # within 10%
        ("Buy $500 of NVDA", order(notional=560), ("DENY", "intent_amount_mismatch")),
        ("Buy $500 of NVDA", order(notional=400), ("ESCALATE", "intent_amount_smaller")),
        ("buy 2 shares of NVDA", order(qty=2), ("ALLOW", "intent_aligned")),
        ("buy 2 shares of NVDA", order(qty=20), ("DENY", "intent_amount_mismatch")),
        ("Trim half of my NVDA", order("sell", qty=5), ("ALLOW", "intent_aligned")),
        ("Trim half of my NVDA", order("sell", qty=10), ("DENY", "intent_amount_mismatch")),
        ("sell NVDA", order("sell", qty=7), ("ALLOW", "intent_aligned")),  # no amount given
    ],
)
def test_amounts(text: str, call: ToolCall, expected: tuple[str, str]) -> None:
    assert check(text, call) == expected


def test_dollars_used_as_shares_is_unit_confusion() -> None:
    assert check("Buy $8 of NVDA", order(qty=8)) == ("ESCALATE", "intent_unit_confusion")


def test_shares_used_as_dollars_is_unit_confusion() -> None:
    assert check("Buy 5 shares of NVDA", order(notional=5)) == ("ESCALATE", "intent_unit_confusion")


def test_unit_confusion_reason_shows_both_readings() -> None:
    result = compare(parse_intent("Buy $8 of NVDA"), order(qty=8), ctx(), tolerance_pct=10)
    assert "$8" in result.reason and "8 shares" in result.reason and "$800" in result.reason


def test_wrong_side_and_symbol_are_denied() -> None:
    assert check("Buy $500 of NVDA", order("sell", qty=2)) == ("DENY", "intent_wrong_side")
    assert check("Buy $500 of AMD", order(notional=500)) == ("DENY", "intent_wrong_symbol")


def test_no_trade_intent_is_denied() -> None:
    assert check("What's NVDA trading at?", order(notional=500)) == ("DENY", "intent_missing")


def test_delegated_symbol_accepts_the_agents_pick() -> None:
    text = "Scan my watchlist and buy $500 of the strongest breakout"
    assert check(text, order(symbol="AMD", notional=500)) == ("ALLOW", "intent_aligned")


def test_matches_the_intent_for_the_orders_symbol() -> None:
    text = "buy $500 of NVDA and $300 of AMD"
    assert check(text, order(symbol="AMD", notional=300)) == ("ALLOW", "intent_aligned")
    assert check(text, order(symbol="AMD", notional=500))[1] == "intent_amount_mismatch"


def test_ambiguous_parse_needs_the_judge() -> None:
    result = compare(parse_intent("buy 1000 NVDA"), order(notional=1000), ctx(), tolerance_pct=10)
    assert result.needs_judge
    assert result.rule_id == "intent_ambiguous_unit"
    assert "$1000" in result.reason and "1000 shares" in result.reason
    assert result.reason.count("has no unit") == 1  # regression: was stated twice
