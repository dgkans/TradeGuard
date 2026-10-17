"""Compare an order with the user's parsed intent."""

from dataclasses import dataclass

from services.gateway.intent.parser import Intent, ParseResult, format_number
from services.gateway.pipeline.types import Decision, PolicyContext, ToolCall


@dataclass(frozen=True)
class Alignment:
    decision: Decision
    rule_id: str
    reason: str
    needs_judge: bool = False


def money(value: float) -> str:
    return f"${value:,.0f}" if value >= 1000 else f"${value:.2f}".rstrip("0").rstrip(".")


def order_numbers(call: ToolCall, price: float) -> tuple[float, float]:
    """(dollars, shares) of the order. Limit orders are sized at the limit price."""
    unit_price = price
    if call.arg("order_type", "market") == "limit" and call.arg("limit_price"):
        unit_price = float(call.arg("limit_price"))
    qty, notional = call.arg("qty"), call.arg("notional")
    if notional is not None:
        return float(notional), float(notional) / unit_price
    shares = float(qty or 0)
    return shares * unit_price, shares


def pick(intents: list[Intent], call: ToolCall) -> Intent | None:
    """The intent this order should be checked against: same symbol, else a delegated one."""
    symbol = str(call.arg("symbol", "")).upper()
    for intent in intents:
        if intent.symbol == symbol:
            return intent
    for intent in intents:
        if intent.symbol is None:
            return intent
    return None


def ambiguous(parsed: ParseResult, call: ToolCall, context: PolicyContext) -> Alignment:
    for intent in parsed.intents:
        if intent.unit is None and intent.amount is not None:
            n = intent.amount
            others = [r for r in parsed.reasons if "has no unit" not in r]
            reason = (
                f"'{format_number(n)}' has no unit: {money(n).replace(',', '')} "
                f"or {format_number(n)} shares ({money(n * context.price)} at "
                f"${context.price:.2f})"
            )
            if others:
                reason += "; " + "; ".join(others)
            return Alignment("ESCALATE", "intent_ambiguous_unit", reason, needs_judge=True)
    reason = "; ".join(parsed.reasons)
    return Alignment(
        "ESCALATE", "intent_ambiguous", f"instruction is ambiguous: {reason}", needs_judge=True
    )


def compare(
    parsed: ParseResult, call: ToolCall, context: PolicyContext, tolerance_pct: float
) -> Alignment:
    """Deterministic alignment of one `place_order` call with the latest user message."""
    if parsed.ambiguous:
        return ambiguous(parsed, call, context)
    if not parsed.intents:
        return Alignment("DENY", "intent_missing", "the user's latest message asks for no trade")
    symbol = str(call.arg("symbol", "")).upper()
    side = call.arg("side")
    intent = pick(parsed.intents, call)
    if intent is None:
        wanted = ", ".join(sorted({i.symbol or "?" for i in parsed.intents}))
        return Alignment(
            "DENY",
            "intent_wrong_symbol",
            f"order is for {symbol} but the user asked about {wanted}",
        )
    if intent.side != side:
        return Alignment(
            "DENY",
            "intent_wrong_side",
            f"order is a {side} but the user asked to {intent.side} {symbol}",
        )
    return check_amount(intent, call, context, tolerance_pct)


def check_amount(
    intent: Intent, call: ToolCall, context: PolicyContext, tolerance_pct: float
) -> Alignment:
    symbol = str(call.arg("symbol", "")).upper()
    if intent.amount is None or intent.unit is None:
        return Alignment("ALLOW", "intent_aligned", f"{intent.side} {symbol}, no size given")
    dollars, shares = order_numbers(call, context.price)
    asked = intent.amount
    if intent.unit == "usd":
        if call.arg("qty") is not None and abs(shares - asked) < 1e-9:
            return confusion(
                f"user asked for ${format_number(asked)} but the order is for "
                f"{format_number(shares)} shares ({money(dollars)})"
            )
        return within(dollars, asked, tolerance_pct, f"{money(dollars)} vs {money(asked)} asked")
    if intent.unit == "shares":
        if call.arg("notional") is not None and abs(dollars - asked) < 1e-9:
            return confusion(
                f"user asked for {format_number(asked)} shares "
                f"({money(asked * context.price)}) but the order is for "
                f"${format_number(asked)}"
            )
        return within(
            shares, asked, tolerance_pct, f"{shares:g} shares vs {format_number(asked)} asked"
        )
    expected = context.held_qty * asked / 100
    if expected <= 0:
        return Alignment(
            "DENY", "intent_amount_mismatch", f"user asked for {asked:g}% of a position of 0 shares"
        )
    return within(
        shares,
        expected,
        tolerance_pct,
        f"{shares:g} shares vs {asked:g}% of {context.held_qty:g} = {expected:g}",
    )


def confusion(detail: str) -> Alignment:
    return Alignment("ESCALATE", "intent_unit_confusion", f"dollars vs shares: {detail}")


def within(actual: float, asked: float, tolerance_pct: float, detail: str) -> Alignment:
    change = (actual - asked) / asked * 100
    if change > tolerance_pct:
        return Alignment(
            "DENY", "intent_amount_mismatch", f"order is {change:.0f}% larger than asked ({detail})"
        )
    if change < -tolerance_pct:
        return Alignment(
            "ESCALATE",
            "intent_amount_smaller",
            f"order is {-change:.0f}% smaller than asked ({detail})",
        )
    return Alignment(
        "ALLOW", "intent_aligned", f"size matches within {tolerance_pct:g}% ({detail})"
    )
