"""Intent stage: when the judge runs, and how its verdict maps to a decision."""

from pathlib import Path
from typing import Any

import pytest

from services.gateway.intent.judge import JudgeVerdict, parse_judge_output
from services.gateway.intent.stage import IntentStage
from services.gateway.pipeline.types import PolicyContext, ToolCall
from services.gateway.policy.model import IntentConfig, JudgeConfig, Policy, load_policy

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = load_policy(REPO_ROOT / "policies/default.yaml")


def judge_may_allow(policy: Policy) -> Policy:
    intent = IntentConfig(enabled=True, judge=JudgeConfig(can_allow=True))
    return policy.model_copy(update={"intent": intent})


CTX = PolicyContext(price=100, held_qty=10, positions_value=1000, equity=20000,
                    market_open=True, orders_today=0)  # fmt: skip


class FakeJudge:
    def __init__(self, aligned: bool = True, confidence: float = 0.9, fail: bool = False) -> None:
        self.calls = 0
        self.aligned = aligned
        self.confidence = confidence
        self.fail = fail

    async def judge(self, user_text: str, call: ToolCall, context: PolicyContext) -> JudgeVerdict:
        self.calls += 1
        if self.fail:
            raise ConnectionError("ollama down")
        return JudgeVerdict(aligned=self.aligned, confidence=self.confidence, reason="because",
                            intent=None, prompt_version="judge_v1", model="fake")  # fmt: skip


def buy(**amount: Any) -> ToolCall:
    return ToolCall("place_order", {"symbol": "NVDA", "side": "buy", **amount})


async def run(
    text: str,
    call: ToolCall,
    judge: FakeJudge | None,
    skip: bool = False,
    policy: Policy = POLICY,
) -> Any:
    return await IntentStage(policy, judge).evaluate(call, CTX, text, skip_approval=skip)


async def test_unambiguous_cases_never_call_the_judge() -> None:
    judge = FakeJudge()
    for text, call in [
        ("Buy $500 of NVDA", buy(notional=500)),
        ("Buy $500 of AMD", buy(notional=500)),
        ("Buy $8 of NVDA", buy(qty=8)),
        ("what's the price?", buy(notional=10)),
    ]:
        verdict = await run(text, call, judge)
        assert verdict.attributes["tradeguard.intent.judge_ran"] is False
        assert "judge: not needed" in verdict.reason
    assert judge.calls == 0


async def test_ambiguous_unit_escalates_even_if_judge_agrees() -> None:
    judge = FakeJudge(aligned=True, confidence=0.99)
    verdict = await run("buy 1000 NVDA", buy(notional=1000), judge)
    assert (verdict.decision, verdict.rule_id) == ("ESCALATE", "intent_ambiguous_unit")
    assert judge.calls == 1
    assert verdict.attributes["tradeguard.intent.judge_ran"] is True
    assert verdict.attributes["tradeguard.intent.judge_prompt"] == "judge_v1"
    assert "judge_v1" in verdict.reason and "fake" in verdict.reason


async def test_ambiguous_unit_denied_when_judge_is_confident_it_is_wrong() -> None:
    verdict = await run("buy 1000 NVDA", buy(qty=1000), FakeJudge(aligned=False, confidence=0.9))
    assert (verdict.decision, verdict.rule_id) == ("DENY", "intent_judge_misaligned")


@pytest.mark.parametrize(
    ("aligned", "confidence", "expected"),
    [
        (True, 0.9, ("ALLOW", "intent_judge_aligned")),
        (True, 0.5, ("ESCALATE", "intent_judge_uncertain")),
        (False, 0.9, ("DENY", "intent_judge_misaligned")),
        (False, 0.5, ("ESCALATE", "intent_judge_uncertain")),
    ],
)
async def test_vague_instruction_follows_the_judge_when_allowed_to(
    aligned: bool, confidence: float, expected: tuple[str, str]
) -> None:
    judge = FakeJudge(aligned=aligned, confidence=confidence)
    policy = judge_may_allow(POLICY)
    verdict = await run("Double my Nvidia position", buy(qty=10), judge, policy=policy)
    assert (verdict.decision, verdict.rule_id) == expected
    assert judge.calls == 1


async def test_default_policy_judge_cannot_allow() -> None:
    """Regression (D012): qwen3:8b judged 5 of 6 misaligned ambiguous cases as confidently
    aligned. By default the judge advises the human and can deny, but never allows."""
    assert POLICY.intent.judge.can_allow is False
    verdict = await run("Double my Nvidia position", buy(qty=10), FakeJudge(True, 0.99))
    assert (verdict.decision, verdict.rule_id) == ("ESCALATE", "intent_judge_uncertain")
    assert "may not allow" in verdict.reason
    denied = await run("Double my Nvidia position", buy(qty=10), FakeJudge(False, 0.9))
    assert denied.decision == "DENY"


async def test_judge_failure_or_absence_escalates() -> None:
    failed = await run("Double my Nvidia position", buy(qty=10), FakeJudge(fail=True))
    assert (failed.decision, failed.rule_id) == ("ESCALATE", "intent_judge_unavailable")
    missing = await run("Double my Nvidia position", buy(qty=10), None)
    assert missing.rule_id == "intent_judge_unavailable"


async def test_human_approval_overrides_intent_escalation_but_not_denial() -> None:
    escalated = await run("Buy $8 of NVDA", buy(qty=8), FakeJudge(), skip=True)
    assert escalated.decision == "ALLOW"
    denied = await run("Buy $500 of AMD", buy(notional=500), FakeJudge(), skip=True)
    assert denied.decision == "DENY"


def test_parse_judge_output() -> None:
    raw = '{"aligned": true, "confidence": 0.82, "reason": "matches", "intent": {"side": "buy"}}'
    verdict = parse_judge_output(raw, prompt_version="judge_v1", model="m")
    assert (verdict.aligned, verdict.confidence, verdict.intent) == (True, 0.82, {"side": "buy"})
    clamped = parse_judge_output('{"aligned": false, "confidence": 7, "reason": "x"}', "v", "m")
    assert clamped.confidence == 1.0
    with pytest.raises(ValueError):
        parse_judge_output("not json", "v", "m")
    with pytest.raises(ValueError):
        parse_judge_output('{"confidence": 0.5}', "v", "m")
