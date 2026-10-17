"""Value types shared by the pipeline stages and rules."""

from dataclasses import dataclass
from typing import Any, Literal

Decision = Literal["ALLOW", "DENY", "ESCALATE"]


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict[str, Any]

    def arg(self, name: str, default: Any = None) -> Any:
        return self.args.get(name, default)


@dataclass(frozen=True)
class PolicyContext:
    """Account and market facts a rule needs, read from the upstream at decision time."""

    price: float  # latest known trade price of the order's symbol
    held_qty: float  # shares of the symbol currently held
    positions_value: float  # market value of all positions
    equity: float  # cash + positions value
    market_open: bool
    orders_today: int  # non-rejected orders submitted on the current (sim) trading day


@dataclass(frozen=True)
class RuleResult:
    decision: Decision
    reason: str
    rule_id: str

    @property
    def passed(self) -> bool:
        return self.decision == "ALLOW"


@dataclass(frozen=True)
class Verdict:
    """Final pipeline decision for one call."""

    decision: Decision
    rule_id: str
    reason: str
