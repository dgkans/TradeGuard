"""Taint rule (pipeline stage 4): a write call after untrusted content needs user support.

- No untrusted content in the session: pass (`taint_clean`).
- The latest user message asks for this side and names this symbol: ALLOW
  (`taint_supported`), even though news was read.
- It asks for the side but doesn't name the symbol (the agent picked it): ESCALATE
  (`taint_partial_support`), or DENY (`taint_high_risk`) if the untrusted content looks
  like an injection (risk at or above the policy threshold).
- Anything else: DENY (`taint_unsupported`). That includes "no user message at all".
"""

from typing import Any

from services.gateway.pipeline.types import RuleResult, ToolCall
from services.gateway.policy.model import Policy
from services.gateway.provenance.store import SessionProvenance
from services.gateway.provenance.support import assess_support


class TaintRule:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def evaluate(self, call: ToolCall, provenance: SessionProvenance) -> RuleResult:
        if not provenance.untrusted_seen():
            return RuleResult("ALLOW", "no untrusted content in this session", "taint_clean")
        sources = sorted({entry.source for entry in provenance.untrusted()})
        risk = provenance.max_risk()
        context = f"after untrusted content from {', '.join(sources)} (max risk {risk:.2f}"
        signals = provenance.signals()
        context += f", signals: {', '.join(signals)})" if signals else ")"
        support = assess_support(provenance.latest_user_text(), call)
        if support.level == "full":
            return RuleResult("ALLOW", f"{support.explanation} {context}", "taint_supported")
        if support.level == "partial":
            if risk >= self.policy.taint.high_risk_threshold:
                return RuleResult("DENY", f"{support.explanation}, {context}", "taint_high_risk")
            return RuleResult("ESCALATE", f"{support.explanation}, {context}",
                              "taint_partial_support")  # fmt: skip
        return RuleResult("DENY", f"{support.explanation} {context}", "taint_unsupported")

    def attributes(self, call: ToolCall, provenance: SessionProvenance) -> dict[str, Any]:
        """Span attributes describing the taint check."""
        support = assess_support(provenance.latest_user_text(), call)
        return {
            "tradeguard.taint.tainted": provenance.untrusted_seen(),
            "tradeguard.taint.support": support.level,
            "tradeguard.taint.max_risk": provenance.max_risk(),
            "tradeguard.taint.sources": ",".join(
                sorted({entry.source for entry in provenance.untrusted()})
            ),
        }
