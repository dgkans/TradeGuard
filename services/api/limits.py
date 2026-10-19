"""A trader's own limits (F10d): what they may change, within what bounds, and what stays on.

A trader edits the numbers a broker's risk desk would let a customer set. The protections
that guard the trader from the AI itself (taint and intent checks), plus market hours and
the cash account's no-shorting rule, are not editable: the API rejects any attempt.
"""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.gateway.policy.model import Policy

ROOT = Path(__file__).resolve().parents[2]
LOCKED = [
    "No short selling",
    "Only while the market is open",
    "Orders you didn't ask for are blocked",
    "Each order must match what you asked",
]


def universe() -> list[str]:
    """Stocks the demo market has prices for (both daily and 5 minute bars)."""
    daily = {path.stem for path in (ROOT / "fixtures/bars/1d").glob("*.parquet")}
    intraday = {path.stem for path in (ROOT / "fixtures/bars/5m").glob("*.parquet")}
    return sorted(daily & intraday)


class Limits(BaseModel):
    """The editable part of a policy. Extra fields (e.g. allow_short, taint) are refused."""

    model_config = ConfigDict(extra="forbid")

    max_order_usd: float = Field(ge=100, le=100_000)
    max_position_usd: float = Field(ge=100, le=500_000)
    max_exposure_pct: float = Field(ge=5, le=100)
    max_orders_per_day: int = Field(ge=1, le=100)
    approval_over_usd: float = Field(ge=0, le=100_000)
    approval_sell_pct: float = Field(ge=1, le=100)
    allowed_symbols: list[str] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def consistent(self) -> "Limits":
        if self.max_position_usd < self.max_order_usd:
            raise ValueError("max_position_usd must be at least max_order_usd")
        if self.approval_over_usd > self.max_order_usd:
            raise ValueError("approval_over_usd must not exceed max_order_usd")
        known = set(universe())
        symbols = []
        for symbol in self.allowed_symbols:
            cleaned = symbol.strip().upper()
            if cleaned not in known:
                raise ValueError(f"allowed_symbols: no prices for {cleaned!r} in this market")
            if cleaned not in symbols:
                symbols.append(cleaned)
        self.allowed_symbols = symbols
        return self


def limits_of(policy: Policy) -> Limits:
    return Limits(
        max_order_usd=policy.max_order_notional_usd,
        max_position_usd=policy.max_position_notional_usd,
        max_exposure_pct=policy.max_portfolio_exposure_pct,
        max_orders_per_day=policy.max_orders_per_day,
        approval_over_usd=policy.require_approval.notional_over_usd,
        approval_sell_pct=policy.require_approval.any_sell_over_pct_of_position,
        allowed_symbols=list(policy.allowed_symbols),
    )


def with_limits(policy: Policy, limits: Limits, owner: str) -> Policy:
    """The trader's policy: the base policy with their limits, validated again like any
    policy file. Protections, tool allowlists and trust labels are kept from the base."""
    data: dict[str, Any] = policy.model_dump()
    data.update({
        "name": f"custom ({owner})",
        "max_order_notional_usd": limits.max_order_usd,
        "max_position_notional_usd": limits.max_position_usd,
        "max_portfolio_exposure_pct": limits.max_exposure_pct,
        "max_orders_per_day": limits.max_orders_per_day,
        "require_approval": {
            "notional_over_usd": limits.approval_over_usd,
            "any_sell_over_pct_of_position": limits.approval_sell_pct,
        },
        "allowed_symbols": list(limits.allowed_symbols),
    })  # fmt: skip
    return Policy.model_validate(data)
