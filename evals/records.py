"""What one eval run produces."""

from dataclasses import asdict, dataclass, field
from typing import Any

# Outcomes of a write call attempted by the agent.
EXECUTED = ("executed", "escalated_executed")
STOPPED = (
    "denied",
    "escalated_rejected",
    "escalated_expired",
    "escalated_blocked",
    "escalated_pending",
    "escalated_unqueued",
)
ESCALATED = (
    "escalated_executed",
    "escalated_rejected",
    "escalated_expired",
    "escalated_blocked",
    "escalated_pending",
    "escalated_unqueued",
)


@dataclass
class WriteAttempt:
    tool: str
    args: dict[str, Any]
    # executed | denied | escalated_* | broker_rejected | error
    outcome: str
    rule_id: str | None
    safe: bool  # matches one of the scenario's must_execute patterns
    dollars: float | None


@dataclass
class RunRecord:
    scenario_id: str
    category: str
    held_out: bool
    config: str
    repeat: int
    writes: list[WriteAttempt]
    task_success: bool
    failures: list[str]
    answer: str
    agent_tokens: int
    judge_tokens: int
    judge_calls: int
    overheads_ms: list[float]
    seconds: float
    error: str | None
    tool_calls: list[str] = field(default_factory=list)

    @property
    def run_key(self) -> str:
        return f"{self.scenario_id}__{self.config}__r{self.repeat}"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RunRecord":
        writes = [WriteAttempt(**w) for w in data["writes"]]
        return cls(**{**data, "writes": writes})
