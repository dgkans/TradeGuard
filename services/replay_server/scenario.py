"""Scenario YAML model and loader (format in PLAN.md Section 7.2)."""

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Category = Literal["benign", "out_of_policy", "injection", "ambiguous", "adversarial_user", "demo"]


class ScenarioError(Exception):
    """A scenario could not be found or is invalid."""


class CallPattern(BaseModel):
    """Matches a write call. `where` keys: symbol, side (exact), usd_between [low, high]."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    where: dict[str, Any] = Field(default_factory=dict)


class Expected(BaseModel):
    """What a correct run looks like (PLAN.md Section 7.2).

    - must_execute: each pattern is matched by at least one executed call.
    - must_not_execute (alias must_not_call): no executed call matches. Attempts the guard
      stopped are fine.
    - Any executed write that matches no must_execute pattern also fails the scenario
      (unexpected writes count as unsafe).
    - must_answer_mentions: substrings the final answer must contain (case insensitive).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    must_execute: list[CallPattern] = Field(default_factory=list)
    must_not_execute: list[CallPattern] = Field(default_factory=list, alias="must_not_call")
    must_answer_mentions: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    """One replayable situation. Fields beyond F1's needs are carried for the eval harness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    category: Category
    seed: int
    start_time: datetime
    watchlist: list[str]
    starting_cash: float = Field(gt=0)
    positions: dict[str, float] = Field(default_factory=dict)
    news_fixtures: list[str] = Field(default_factory=list)
    user_turns: list[str] = Field(default_factory=list)
    approver: Literal["approve", "reject", "timeout"] = "reject"
    expected: Expected = Field(default_factory=Expected)
    labels: dict[str, Any] = Field(default_factory=dict)
    # Held out scenarios are reported separately and never used for tuning (PLAN.md 7.4).
    held_out: bool = False
    between_turns_minutes: int = Field(default=0, ge=0)
    description: str = ""

    @field_validator("start_time")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("start_time needs a timezone, for example 2026-09-15T14:30:00Z")
        return value

    @field_validator("watchlist")
    @classmethod
    def upper_symbols(cls, value: list[str]) -> list[str]:
        cleaned = []
        for symbol in value:
            cleaned.append(symbol.strip().upper())
        return cleaned


def load_scenario(name: str, root: Path = Path("scenarios")) -> Scenario:
    """Load a scenario by file path or by id (searches `root` recursively for `<id>.yaml`).

    Raises ScenarioError when nothing or more than one file matches, or the YAML is invalid.
    """
    path = Path(name)
    if path.suffix not in (".yaml", ".yml") or not path.is_file():
        matches = sorted(root.rglob(f"{name}.yaml"))
        if not matches:
            raise ScenarioError(f"no scenario named {name!r} under {root}")
        if len(matches) > 1:
            raise ScenarioError(f"scenario {name!r} is ambiguous: {matches}")
        path = matches[0]

    try:
        data = yaml.safe_load(path.read_text())
        return Scenario.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ScenarioError(f"invalid scenario {path}: {exc}") from exc
