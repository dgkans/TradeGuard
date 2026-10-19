"""Plain-language lines for the trader view (F10b).

The agent's tool calls and the gateway's decisions arrive as structured data and JSON
refusals. Traders see one sentence per step instead, plus a short title and reason when
the guard stops something. Nothing here decides anything; it only describes.
"""

import json
import re
from typing import Any

# Short, trader facing titles for every rule that can stop or hold an order.
RULE_TITLES: dict[str, str] = {
    "max_order_notional": "Order is over your per-order limit",
    "max_position_notional": "Would make this position too large",
    "max_portfolio_exposure": "Would put too much of your account in stocks",
    "max_orders_per_day": "Daily order limit reached",
    "market_hours": "The market is closed",
    "symbol_not_allowed": "This stock isn't on your allowed list",
    "order_type_not_allowed": "This order type isn't allowed",
    "order_shape": "The order isn't valid",
    "no_short": "Selling more than you own isn't allowed",
    "approval_notional": "Large order: needs your approval",
    "approval_sell_pct": "Large sale: needs your approval",
    "approval_unavailable": "Approvals are unavailable, so it was stopped",
    "approval_timeout": "No one approved it in time",
    "human_rejection": "Rejected by a reviewer",
    "audit_unavailable": "The safety log is unavailable, so it was stopped",
    "taint_unsupported": "You didn't ask for this order",
    "taint_partial_support": "This doesn't fully match what you asked",
    "taint_high_risk": "Suspicious instructions were found in the data",
    "intent_missing": "You didn't ask for a trade",
    "intent_wrong_side": "This is the opposite of what you asked (buy or sell)",
    "intent_wrong_symbol": "This is a different stock from the one you asked for",
    "intent_amount_mismatch": "This is a different amount from what you asked",
    "intent_amount_smaller": "This is smaller than what you asked",
    "intent_unit_confusion": "Shares and dollars look mixed up",
    "intent_ambiguous_unit": "Unclear whether you meant shares or dollars",
    "intent_judge_uncertain": "Not sure this matches your request",
    "intent_judge_misaligned": "This doesn't match your request",
    "intent_judge_unavailable": "Couldn't confirm this matches your request",
}

VERBS = {
    "get_quote": "check",
    "get_bars": "load the price history for",
    "get_news": "read the news for",
    "detect_patterns": "scan",
    "place_order": "place the order for",
    "cancel_order": "cancel the order",
}


def money(value: Any) -> str:
    number = float(value)
    text = f"${number:,.2f}"
    return text[:-3] if text.endswith(".00") else text


def plain(text: Any) -> str:
    """Strip characters that make text look like code, keep the words."""
    return str(text).replace('"', "'").replace("{", "").replace("}", "").strip()


# Policy setting names as a trader would say them. Order matters: longer phrases first.
SETTING_WORDS = [
    ("exceeds max_order_notional_usd", "is over your per-order limit of"),
    ("over max_position_notional_usd", "over your position limit of"),
    ("over max_portfolio_exposure_pct", "over your limit of"),
    ("max_orders_per_day is", "your daily limit is"),
]
AMOUNT = re.compile(r"\$(\d+(?:\.\d+)?)")


def friendly_reason(text: str) -> str:
    """A gateway reason without setting names, with amounts as $3,000 instead of $3000.00."""
    out = plain(text)
    for setting, words in SETTING_WORDS:
        out = out.replace(setting, words)
    out = AMOUNT.sub(lambda match: money(match.group(1)), out)
    out = re.sub(r"\b([a-z]+(?:_[a-z]+)+)\b", lambda m: m.group(1).replace("_", " "), out)
    return out


def order_text(args: dict[str, Any]) -> str:
    """'buy $1,500 of NVDA at market' or 'sell 2 shares of AMD, limit $150'."""
    side = str(args.get("side", "")).lower() or "trade"
    symbol = str(args.get("symbol", "")).upper()
    if args.get("notional") is not None:
        size = money(args["notional"])
    elif args.get("qty") is not None:
        qty = float(args["qty"])
        shown = int(qty) if qty.is_integer() else qty
        size = f"{shown} share{'' if qty == 1 else 's'}"
    else:
        size = "an amount"
    if str(args.get("order_type", "market")) == "limit" and args.get("limit_price") is not None:
        return f"{side} {size} of {symbol}, limit {money(args['limit_price'])}"
    return f"{side} {size} of {symbol} at market"


def ok_text(tool: str, args: dict[str, Any], result: Any) -> str:
    data = result if isinstance(result, dict) else {}
    symbol = str(args.get("symbol") or data.get("symbol") or "").upper()
    if tool == "get_quote" and "price" in data:
        return f"Checked {symbol}: {money(data['price'])}"
    if tool == "get_account" and "cash" in data:
        return f"Checked your account: {money(data['cash'])} cash"
    if tool == "get_positions":
        return f"Checked your positions ({len(data.get('positions') or [])})"
    if tool == "list_orders":
        return f"Checked your orders ({len(data.get('orders') or [])})"
    if tool == "get_news":
        count = len(data.get("items") or [])
        target = f" for {symbol}" if symbol else ""
        return f"Read {count} news item{'' if count == 1 else 's'}{target}"
    if tool == "get_bars":
        interval = "5 minute" if args.get("interval") == "5m" else "daily"
        return f"Looked at {symbol}'s {interval} price history"
    if tool == "detect_patterns":
        return f"Scanned {symbol} for chart patterns"
    if tool == "scan_watchlist":
        return f"Scanned {len(args.get('symbols') or [])} stocks for chart patterns"
    if tool == "place_order":
        return f"Order sent: {order_text(args)}"
    if tool == "cancel_order":
        return f"Cancelled your {symbol} order".replace("your  order", "your order")
    if tool == "get_clock":
        return "Checked the market clock: " + ("open" if data.get("market_open") else "closed")
    return f"Used {tool.replace('_', ' ')}"


def refusal_of(error: str) -> dict[str, Any] | None:
    """The gateway's DENY or ESCALATE body inside a tool error, if it is one."""
    text = error.removeprefix("ERROR: ").strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        body = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    if isinstance(body, dict) and body.get("decision") in ("DENY", "ESCALATE"):
        return body
    return None


def describe_step(
    tool: str, args: dict[str, Any], result: Any, error: str | None
) -> dict[str, Any]:
    """One trader facing line for a tool call: status ok, blocked, held or failed."""
    if error is None:
        data = result if isinstance(result, dict) else {}
        if tool == "place_order" and data.get("status") == "rejected":
            why = plain(data.get("reject_reason") or "no reason given")
            return {"status": "failed", "tool": tool,
                    "text": f"The broker rejected: {order_text(args)} ({why})"}  # fmt: skip
        return {"status": "ok", "tool": tool, "text": ok_text(tool, args, result)}
    body = refusal_of(error)
    if body is not None:
        rule = str(body.get("rule_id", ""))
        what = order_text(args) if tool == "place_order" else tool.replace("_", " ")
        held = body["decision"] == "ESCALATE"
        return {
            "status": "held" if held else "blocked",
            "tool": tool,
            "text": f"{'Waiting for your approval' if held else 'Blocked'}: {what}",
            "title": RULE_TITLES.get(rule, "Stopped by your guard"),
            "reason": friendly_reason(str(body.get("reason", ""))),
            "rule_id": rule,
        }
    message = plain(error.split(":", 1)[-1] if error.startswith("Error executing tool") else error)
    target = str(args.get("symbol", "")).upper() or tool.replace("_", " ")
    verb = VERBS.get(tool, "use")
    if tool == "get_quote":
        return {"status": "failed", "tool": tool, "text": f"Couldn't check {target}: {message}"}
    return {"status": "failed", "tool": tool, "text": f"Couldn't {verb} {target}: {message}"}


DECISION_TEXT = {
    "executed": "Approved. The order was sent.",
    "rejected": "Rejected. Nothing was sent.",
    "expired": "The approval expired. Nothing was sent.",
    "blocked": "Approved, but blocked on a final check. Nothing was sent.",
}


def describe_decision(outcome: dict[str, Any]) -> dict[str, Any]:
    status = str(outcome.get("status", ""))
    text = DECISION_TEXT.get(status, f"Approval {status}.")
    who = outcome.get("decided_by")
    if who and status in ("executed", "rejected", "blocked"):
        text += f" Decided by {plain(who)}."
    note = outcome.get("note")
    if note:
        text += f" Note: {plain(note)}"
    return {"status": status, "text": text}
