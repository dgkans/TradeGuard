"""Plain-language lines for the trader view: no JSON, ever."""

import json

import pytest

from services.api.describe import RULE_TITLES, describe_decision, describe_step


def refusal(decision: str, rule_id: str, reason: str, **extra: object) -> str:
    return json.dumps({"decision": decision, "rule_id": rule_id, "reason": reason,
                       "tool": "place_order", "executed": False, **extra})  # fmt: skip


def no_json(text: str) -> None:
    assert "{" not in text and "}" not in text and '"' not in text, text


@pytest.mark.parametrize(
    ("tool", "args", "result", "expected"),
    [
        ("get_quote", {"symbol": "NVDA"}, {"symbol": "NVDA", "price": 212.74},
         "Checked NVDA: $212.74"),
        ("get_account", {}, {"cash": 25000.0, "equity": 28774.65},
         "Checked your account: $25,000 cash"),
        ("get_positions", {}, {"positions": [{"symbol": "NVDA"}, {"symbol": "AAPL"}]},
         "Checked your positions (2)"),
        ("list_orders", {"status": "all"}, {"orders": []}, "Checked your orders (0)"),
        ("get_news", {"symbol": "AMD"}, {"items": [{}, {}]}, "Read 2 news items for AMD"),
        ("get_bars", {"symbol": "TSLA", "interval": "5m"}, {"bars": []},
         "Looked at TSLA's 5 minute price history"),
        ("detect_patterns", {"symbol": "AMD"}, {"fired": []}, "Scanned AMD for chart patterns"),
        ("scan_watchlist", {"symbols": ["NVDA", "AMD"]}, {"results": []},
         "Scanned 2 stocks for chart patterns"),
        ("place_order", {"symbol": "NVDA", "side": "buy", "notional": 500},
         {"status": "new", "symbol": "NVDA"}, "Order sent: buy $500 of NVDA at market"),
        ("place_order", {"symbol": "AMD", "side": "sell", "qty": 2, "order_type": "limit",
                         "limit_price": 150}, {"status": "new"},
         "Order sent: sell 2 shares of AMD, limit $150"),
        ("cancel_order", {"order_id": "ord_1"}, {"status": "canceled", "symbol": "AMD"},
         "Cancelled your AMD order"),
    ],
)  # fmt: skip
def test_successful_steps_read_like_sentences(
    tool: str, args: dict[str, object], result: dict[str, object], expected: str
) -> None:
    step = describe_step(tool, args, result, None)
    assert step == {"status": "ok", "text": expected, "tool": tool}
    no_json(step["text"])


def test_blocked_order_gets_a_title_and_the_reason() -> None:
    error = refusal("DENY", "max_order_notional", "order size $3000.00 is over the limit $2000.00")
    step = describe_step("place_order", {"symbol": "TSLA", "side": "buy", "notional": 3000},
                         None, error)  # fmt: skip
    assert step["status"] == "blocked"
    assert step["title"] == RULE_TITLES["max_order_notional"]
    assert step["text"] == "Blocked: buy $3,000 of TSLA at market"
    assert step["reason"] == "order size $3,000 is over the limit $2,000"
    for value in step.values():
        no_json(str(value))


def test_held_order_says_it_waits_for_approval() -> None:
    error = refusal("ESCALATE", "approval_notional", "over the approval threshold",
                    approval_id="apr_1", status="pending")  # fmt: skip
    step = describe_step("place_order", {"symbol": "NVDA", "side": "buy", "notional": 1500},
                         None, error)  # fmt: skip
    assert step["status"] == "held"
    assert step["text"] == "Waiting for your approval: buy $1,500 of NVDA at market"


def test_tool_input_errors_are_plain_too() -> None:
    step = describe_step("get_quote", {"symbol": "ZZZZ"}, None,
                         "Error executing tool get_quote: unknown symbol ZZZZ")  # fmt: skip
    assert step["status"] == "failed"
    assert step["text"] == "Couldn't check ZZZZ: unknown symbol ZZZZ"


def test_broker_rejection_is_a_failure_not_a_success() -> None:
    step = describe_step(
        "place_order",
        {"symbol": "AMD", "side": "sell", "qty": 50},
        {"status": "rejected", "reject_reason": "insufficient shares"},
        None,
    )
    assert step["status"] == "failed"
    assert (
        step["text"] == "The broker rejected: sell 50 shares of AMD at market (insufficient shares)"
    )


BLOCKING_RULES = [
    "approval_notional", "approval_sell_pct", "approval_unavailable", "audit_unavailable",
    "human_rejection", "approval_timeout", "intent_ambiguous_unit", "intent_amount_mismatch",
    "intent_amount_smaller", "intent_judge_misaligned", "intent_judge_unavailable",
    "intent_judge_uncertain", "intent_missing", "intent_unit_confusion", "intent_wrong_side",
    "intent_wrong_symbol", "market_hours", "max_order_notional", "max_orders_per_day",
    "max_portfolio_exposure", "max_position_notional", "no_short", "order_shape",
    "order_type_not_allowed", "symbol_not_allowed", "taint_high_risk", "taint_partial_support",
    "taint_unsupported",
]  # fmt: skip


def test_every_rule_that_can_stop_an_order_has_a_plain_title() -> None:
    missing = [rule for rule in BLOCKING_RULES if rule not in RULE_TITLES]
    assert missing == []
    for title in RULE_TITLES.values():
        no_json(title)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("executed", "Approved. The order was sent."),
        ("rejected", "Rejected. Nothing was sent."),
        ("expired", "The approval expired. Nothing was sent."),
        ("blocked", "Approved, but blocked on a final check. Nothing was sent."),
    ],
)
def test_decisions_read_like_sentences(status: str, expected: str) -> None:
    assert describe_decision({"status": status, "decided_by": "alice"})["text"].startswith(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("order size $3000.00 exceeds max_order_notional_usd $2000.00",
         "order size $3,000 is over your per-order limit of $2,000"),
        ("position would be $5200.50, over max_position_notional_usd $5000.00",
         "position would be $5,200.50, over your position limit of $5,000"),
        ("exposure would be 72.0% of equity, over max_portfolio_exposure_pct 60%",
         "exposure would be 72.0% of equity, over your limit of 60%"),
        ("11 orders already placed today; max_orders_per_day is 10",
         "11 orders already placed today; your daily limit is 10"),
        ("order size $1500.00 is over the approval threshold $1000.00",
         "order size $1,500 is over the approval threshold $1,000"),
        ("NFLX is not in the allowed symbols (NVDA, AMD)",
         "NFLX is not in the allowed symbols (NVDA, AMD)"),
    ],
)  # fmt: skip
def test_reasons_use_trader_words_not_setting_names(raw: str, expected: str) -> None:
    from services.api.describe import friendly_reason

    assert friendly_reason(raw) == expected
    error = refusal("DENY", "max_order_notional", raw)
    step = describe_step("place_order", {"symbol": "TSLA", "side": "buy", "notional": 3000},
                         None, error)  # fmt: skip
    assert step["reason"] == expected
    assert "_" not in step["reason"]
