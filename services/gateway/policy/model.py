"""Policy file model and YAML loader.

Unknown fields are rejected, so a typo like `max_order_notinal_usd` fails loudly instead of
silently leaving a limit unset.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class PolicyError(Exception):
    """The policy file is missing or invalid. The message names the field at fault."""


class ApprovalRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    notional_over_usd: float = Field(ge=0)
    any_sell_over_pct_of_position: float = Field(ge=0, le=100)


class ToolAllowlist(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    read: list[str]
    write: list[str]

    @model_validator(mode="after")
    def disjoint(self) -> "ToolAllowlist":
        both = sorted(set(self.read) & set(self.write))
        if both:
            raise ValueError(f"tools listed as both read and write: {both}")
        return self


Trust = Literal["trusted", "untrusted"]


class TaintConfig(BaseModel):
    """Provenance and taint tracking (F7). Off unless the policy turns it on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    wrap_untrusted: bool = True
    high_risk_threshold: float = Field(default=0.5, ge=0, le=1)
    # Tools not listed here get default_trust. Fail closed: unknown output is untrusted.
    default_trust: Trust = "untrusted"
    trust_labels: dict[str, Trust] = Field(default_factory=dict)

    def trust_of(self, tool: str) -> Trust:
        return self.trust_labels.get(tool, self.default_trust)


class JudgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    min_confidence: float = Field(default=0.8, ge=0, le=1)
    # Whether a confident "aligned" judge verdict may ALLOW an ambiguous order. Off by
    # default: measured judge agreement on ambiguous cases is low (DECISIONS.md D012).
    can_allow: bool = False


class IntentConfig(BaseModel):
    """Intent alignment (F8). Off unless the policy turns it on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    amount_tolerance_pct: float = Field(default=10, ge=0, le=100)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    allowed_symbols: list[str]
    allowed_order_types: list[str]
    max_order_notional_usd: float = Field(gt=0)
    max_position_notional_usd: float = Field(gt=0)
    max_portfolio_exposure_pct: float = Field(gt=0, le=100)
    max_orders_per_day: int = Field(ge=0)
    market_hours_only: bool
    allow_short: bool
    require_approval: ApprovalRules
    tool_allowlist: ToolAllowlist
    # Tools only the human operator may call (with the session operator token).
    operator_tools: list[str] = Field(default_factory=list)
    taint: TaintConfig = Field(default_factory=TaintConfig)
    intent: IntentConfig = Field(default_factory=IntentConfig)

    @field_validator("allowed_symbols")
    @classmethod
    def upper_symbols(cls, value: list[str]) -> list[str]:
        cleaned = []
        for symbol in value:
            cleaned.append(symbol.strip().upper())
        return cleaned

    def is_write_tool(self, tool: str) -> bool:
        return tool in self.tool_allowlist.write

    def is_allowed_tool(self, tool: str) -> bool:
        return tool in self.tool_allowlist.read or tool in self.tool_allowlist.write


def load_policy(path: str | Path) -> Policy:
    """Load and validate a policy YAML file.

    Raises PolicyError for a missing file, invalid YAML or invalid fields. Validation
    messages use dotted field paths, e.g. `require_approval.notional_over_usd`.
    """
    file = Path(path)
    if not file.is_file():
        raise PolicyError(f"policy file not found: {file}")
    try:
        data = yaml.safe_load(file.read_text())
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy {file} is not valid YAML: {exc}") from exc
    try:
        return Policy.model_validate(data)
    except ValidationError as exc:
        lines = [f"invalid policy {file}:"]
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "(top level)"
            lines.append(f"  {field}: {error['msg']}")
        raise PolicyError("\n".join(lines)) from None
