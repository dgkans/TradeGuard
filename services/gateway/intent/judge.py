"""LLM intent judge: only for instructions the deterministic parser marks ambiguous.

One JSON call returns `{aligned, confidence, reason, intent}`. The `intent` field doubles as
the plan's LLM parsing fallback. The prompt version and model are attached to every
verdict, so each decision records which judge made it.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from services.agent.prompts import JUDGE_PROMPT, load_prompt
from services.gateway.pipeline.types import PolicyContext, ToolCall


@dataclass(frozen=True)
class JudgeVerdict:
    aligned: bool
    confidence: float
    reason: str
    intent: dict[str, Any] | None
    prompt_version: str
    model: str


class Judge(Protocol):
    async def judge(
        self, user_text: str, call: ToolCall, context: PolicyContext
    ) -> JudgeVerdict: ...


def judge_input(user_text: str, call: ToolCall, context: PolicyContext) -> str:
    order: dict[str, Any] = {"symbol": call.arg("symbol"), "side": call.arg("side")}
    for key in ("qty", "notional", "order_type", "limit_price"):
        if call.arg(key) is not None:
            order[key] = call.arg(key)
    return json.dumps(
        {
            "user_message": user_text,
            "proposed_order": order,
            "context": {"price": context.price, "held_qty": context.held_qty},
        }
    )


def parse_judge_output(raw: str, prompt_version: str, model: str) -> JudgeVerdict:
    """Validate the judge's JSON. Raises ValueError if it is unusable."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"judge returned no JSON: {raw[:120]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError(f"judge returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("aligned"), bool):
        raise ValueError("judge output has no boolean 'aligned'")
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError) as exc:
        raise ValueError("judge confidence is not a number") from exc
    intent = data.get("intent")
    return JudgeVerdict(
        aligned=data["aligned"],
        confidence=min(max(confidence, 0.0), 1.0),
        reason=str(data.get("reason", ""))[:300],
        intent=intent if isinstance(intent, dict) else None,
        prompt_version=prompt_version,
        model=model,
    )


class LlmJudge:
    def __init__(self, chat: BaseChatModel, model: str, prompt_version: str = JUDGE_PROMPT):
        self.chat = chat
        self.model = model
        self.prompt_version = prompt_version
        self.system = load_prompt(prompt_version)

    async def judge(self, user_text: str, call: ToolCall, context: PolicyContext) -> JudgeVerdict:
        """Ask the model. Raises on transport errors or unusable output (caller escalates)."""
        reply = await self.chat.ainvoke(
            [SystemMessage(self.system), HumanMessage(judge_input(user_text, call, context))]
        )
        content = reply.content if isinstance(reply.content, str) else json.dumps(reply.content)
        return parse_judge_output(content, self.prompt_version, self.model)
