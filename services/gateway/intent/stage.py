"""Pipeline stage 5: does the order match the user's latest instruction?

Deterministic parse and compare first. The judge runs only when the parse is ambiguous.
- Ambiguous unit ("buy 1000 NVDA"): always ESCALATE, with the judge's view attached; the
  judge can turn it into a DENY, never an ALLOW.
- Other ambiguity: a confident "misaligned" judge DENIES. A confident "aligned" judge
  ALLOWS only if the policy sets `judge.can_allow` (off by default); otherwise ESCALATE.
- Judge missing or failing: ESCALATE (a human decides).
After a human approval (`skip_approval`), ESCALATE passes; DENY still blocks.
"""

from typing import Any

from services.gateway.intent.compare import Alignment, compare
from services.gateway.intent.judge import Judge, JudgeVerdict
from services.gateway.intent.parser import parse_intent
from services.gateway.pipeline.types import PolicyContext, ToolCall, Verdict
from services.gateway.policy.model import Policy


class IntentStage:
    def __init__(self, policy: Policy, judge: Judge | None) -> None:
        self.policy = policy
        self.judge = judge

    async def evaluate(
        self, call: ToolCall, context: PolicyContext, user_text: str, *, skip_approval: bool
    ) -> Verdict:
        parsed = parse_intent(user_text)
        alignment = compare(parsed, call, context, self.policy.intent.amount_tolerance_pct)
        attributes: dict[str, Any] = {
            "tradeguard.intent.ambiguous": parsed.ambiguous,
            "tradeguard.intent.judge_ran": False,
            "tradeguard.intent.deterministic_rule": alignment.rule_id,
        }
        if not alignment.needs_judge:
            verdict = Verdict(
                alignment.decision,
                alignment.rule_id,
                f"{alignment.reason}; judge: not needed",
                attributes,
            )
        else:
            verdict = await self.with_judge(alignment, call, context, user_text, attributes)
        if skip_approval and verdict.decision == "ESCALATE":
            return Verdict(
                "ALLOW",
                "intent_approved",
                f"human approved despite: {verdict.reason}",
                verdict.attributes,
            )
        return verdict

    async def with_judge(
        self,
        alignment: Alignment,
        call: ToolCall,
        context: PolicyContext,
        user_text: str,
        attributes: dict[str, Any],
    ) -> Verdict:
        settings = self.policy.intent.judge
        if self.judge is None or not settings.enabled:
            return Verdict(
                "ESCALATE",
                "intent_judge_unavailable",
                f"{alignment.reason}; judge: not configured",
                attributes,
            )
        try:
            result = await self.judge.judge(user_text, call, context)
        except Exception as exc:
            attributes["tradeguard.intent.judge_ran"] = True
            return Verdict(
                "ESCALATE",
                "intent_judge_unavailable",
                f"{alignment.reason}; judge failed: {type(exc).__name__}: {exc}",
                attributes,
            )
        attributes.update(judge_attributes(result))
        view = (
            f"{alignment.reason}; judge ({result.prompt_version}, {result.model}): "
            f"aligned={result.aligned} confidence={result.confidence:.2f}: {result.reason}"
        )
        confident = result.confidence >= settings.min_confidence
        if not result.aligned and confident:
            return Verdict("DENY", "intent_judge_misaligned", view, attributes)
        if alignment.rule_id == "intent_ambiguous_unit":
            return Verdict("ESCALATE", "intent_ambiguous_unit", view, attributes)
        if result.aligned and confident and settings.can_allow:
            return Verdict("ALLOW", "intent_judge_aligned", view, attributes)
        if result.aligned and confident:
            view += "; judge may not allow on its own under this policy, a human decides"
        return Verdict("ESCALATE", "intent_judge_uncertain", view, attributes)


def judge_attributes(result: JudgeVerdict) -> dict[str, Any]:
    return {
        "tradeguard.intent.judge_ran": True,
        "tradeguard.intent.judge_prompt": result.prompt_version,
        "tradeguard.intent.judge_model": result.model,
        "tradeguard.intent.judge_aligned": result.aligned,
        "tradeguard.intent.judge_confidence": result.confidence,
    }
