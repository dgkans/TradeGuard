"""Policy rules for `place_order`. Each rule checks one thing and names itself in `rule_id`.

Static rules need only the call. Context rules also need account and market facts.
Approval rules can only ESCALATE. Boundaries are inclusive: an order exactly at a limit
is allowed; a limit is exceeded only when strictly over it.
"""

from services.gateway.pipeline.types import PolicyContext, RuleResult, ToolCall
from services.gateway.policy.model import Policy

EPSILON = 1e-9  # tolerance for float division in share counts


class Rule:
    rule_id = "rule"

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        raise NotImplementedError

    def allow(self, reason: str) -> RuleResult:
        return RuleResult("ALLOW", reason, self.rule_id)

    def deny(self, reason: str) -> RuleResult:
        return RuleResult("DENY", reason, self.rule_id)

    def escalate(self, reason: str) -> RuleResult:
        return RuleResult("ESCALATE", reason, self.rule_id)


def is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def require(context: PolicyContext | None) -> PolicyContext:
    if context is None:
        raise ValueError("this rule needs account context")
    return context


def order_notional(call: ToolCall, price: float) -> float:
    """Dollar size of the order: notional as given, or qty at the limit (else market) price."""
    notional = call.arg("notional")
    if notional is not None:
        return float(notional)
    limit = call.arg("limit_price")
    unit = float(limit) if call.arg("order_type", "market") == "limit" and limit else price
    return float(call.arg("qty")) * unit


def order_qty(call: ToolCall, price: float) -> float:
    """Shares in the order; dollar orders are converted at the current price."""
    qty = call.arg("qty")
    if qty is not None:
        return float(qty)
    return float(call.arg("notional")) / price


def money(value: float) -> str:
    return f"${value:.2f}"


# --- static rules ---------------------------------------------------------------------


class OrderShape(Rule):
    rule_id = "order_shape"

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        if call.arg("side") not in ("buy", "sell"):
            return self.deny("side must be 'buy' or 'sell'")
        qty, notional = call.arg("qty"), call.arg("notional")
        if (qty is None) == (notional is None):
            return self.deny("give exactly one of qty (shares) or notional (dollars)")
        amount = qty if qty is not None else notional
        if not is_number(amount) or amount <= 0:
            return self.deny("order amount must be a number greater than 0")
        if call.arg("order_type", "market") == "limit":
            limit = call.arg("limit_price")
            if not is_number(limit) or limit <= 0:
                return self.deny("limit orders need a limit_price greater than 0")
            if notional is not None:
                return self.deny("limit orders must use qty, not notional")
        return self.allow("order is well formed")


class SymbolAllowed(Rule):
    rule_id = "symbol_not_allowed"

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        symbol = str(call.arg("symbol", "")).strip().upper()
        if symbol not in self.policy.allowed_symbols:
            allowed = ", ".join(self.policy.allowed_symbols)
            return self.deny(f"{symbol} is not in the allowed symbols ({allowed})")
        return self.allow(f"{symbol} is allowed")


class OrderTypeAllowed(Rule):
    rule_id = "order_type_not_allowed"

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        order_type = call.arg("order_type", "market")
        if order_type not in self.policy.allowed_order_types:
            allowed = ", ".join(self.policy.allowed_order_types)
            return self.deny(f"order type {order_type!r} is not allowed (allowed: {allowed})")
        return self.allow(f"order type {order_type} is allowed")


# --- context rules --------------------------------------------------------------------


class MaxOrderNotional(Rule):
    rule_id = "max_order_notional"

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        notional = order_notional(call, require(context).price)
        limit = self.policy.max_order_notional_usd
        if notional > limit:
            return self.deny(
                f"order size {money(notional)} exceeds max_order_notional_usd {money(limit)}"
            )
        return self.allow(f"order size {money(notional)} is within {money(limit)}")


class NoShort(Rule):
    rule_id = "no_short"

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        ctx = require(context)
        if self.policy.allow_short or call.arg("side") != "sell":
            return self.allow("not a short sale")
        qty = order_qty(call, ctx.price)
        if qty > ctx.held_qty + EPSILON:
            return self.deny(
                f"selling {qty:g} shares but only {ctx.held_qty:g} held; short selling is "
                "not allowed"
            )
        return self.allow(f"selling {qty:g} of {ctx.held_qty:g} held shares")


class MaxPositionNotional(Rule):
    rule_id = "max_position_notional"

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        ctx = require(context)
        if call.arg("side") != "buy":
            return self.allow("sells reduce the position")
        after = ctx.held_qty * ctx.price + order_notional(call, ctx.price)
        limit = self.policy.max_position_notional_usd
        if after > limit:
            return self.deny(
                f"position would be {money(after)}, over max_position_notional_usd {money(limit)}"
            )
        return self.allow(f"position after order {money(after)} is within {money(limit)}")


class MaxPortfolioExposure(Rule):
    rule_id = "max_portfolio_exposure"

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        ctx = require(context)
        if call.arg("side") != "buy":
            return self.allow("sells reduce exposure")
        if ctx.equity <= 0:
            return self.deny("account equity is zero or negative")
        pct = (ctx.positions_value + order_notional(call, ctx.price)) / ctx.equity * 100
        limit = self.policy.max_portfolio_exposure_pct
        if pct > limit + EPSILON:
            return self.deny(
                f"exposure would be {pct:.1f}% of equity, over "
                f"max_portfolio_exposure_pct {limit:g}%"
            )
        return self.allow(f"exposure after order {pct:.1f}% is within {limit:g}%")


class MaxOrdersPerDay(Rule):
    rule_id = "max_orders_per_day"

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        today = require(context).orders_today
        limit = self.policy.max_orders_per_day
        if today >= limit:
            return self.deny(f"{today} orders already placed today; max_orders_per_day is {limit}")
        return self.allow(f"{today} of {limit} orders used today")


class MarketHours(Rule):
    rule_id = "market_hours"

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        if self.policy.market_hours_only and not require(context).market_open:
            return self.deny("the market is closed and market_hours_only is set")
        return self.allow("market hours ok")


# --- approval rules -------------------------------------------------------------------


class ApprovalNotional(Rule):
    rule_id = "approval_notional"

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        notional = order_notional(call, require(context).price)
        threshold = self.policy.require_approval.notional_over_usd
        if notional > threshold:
            return self.escalate(
                f"order size {money(notional)} is over the approval threshold {money(threshold)}"
            )
        return self.allow(f"order size {money(notional)} needs no approval")


class ApprovalSellPct(Rule):
    rule_id = "approval_sell_pct"

    def evaluate(self, call: ToolCall, context: PolicyContext | None) -> RuleResult:
        ctx = require(context)
        if call.arg("side") != "sell" or ctx.held_qty <= 0:
            return self.allow("not selling part of a held position")
        pct = order_qty(call, ctx.price) / ctx.held_qty * 100
        threshold = self.policy.require_approval.any_sell_over_pct_of_position
        if pct > threshold + EPSILON:
            return self.escalate(
                f"selling {pct:.1f}% of the position, over the approval threshold {threshold:g}%"
            )
        return self.allow(f"selling {pct:.1f}% of the position needs no approval")


STATIC_RULES: list[type[Rule]] = [OrderShape, SymbolAllowed, OrderTypeAllowed]
CONTEXT_RULES: list[type[Rule]] = [
    MarketHours, MaxOrdersPerDay, NoShort, MaxOrderNotional, MaxPositionNotional,
    MaxPortfolioExposure,
]  # fmt: skip
APPROVAL_RULES: list[type[Rule]] = [ApprovalNotional, ApprovalSellPct]
