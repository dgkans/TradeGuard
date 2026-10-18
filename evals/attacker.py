"""A scripted worst case agent (F9c): it acts as if every injection fully hijacked it.

With a real model, the guard is only tested when the model takes the bait, and qwen3:8b
rarely does (RESULTS.md, F9 and F9b). This agent always does, so it measures what the
guard stops on its own. For each injection scenario it follows
`evals/datasets/attack_payloads.yaml`:

    turn 1: read the news for the scenario's symbols (the payload enters the context)
    last turn: place the attacker's order(s), then the user's own order(s), then answer

It runs through the real agent graph, tools and gateway; only the model is replaced by a
fixed sequence of messages. No LLM, no tokens, the same calls every run.
"""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from services.replay_server.scenario import Scenario

AGENT_NAME = "scripted:worst_case_attacker"
AgentFactory = Callable[[Scenario], tuple[BaseChatModel, BaseChatModel]]


class ScriptedAgentError(RuntimeError):
    """The agent graph asked for more messages than the script has."""


class ScriptedAgent(BaseChatModel):
    """Returns the scripted messages in order. Used for both agent roles."""

    _responses: list[AIMessage] = PrivateAttr(default_factory=list)

    def __init__(self, responses: Sequence[AIMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "scripted_attacker"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedAgent":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if not self._responses:
            raise ScriptedAgentError("the script has no more messages")
        return ChatResult(generations=[ChatGeneration(message=self._responses.pop(0))])


def load_labels(path: Path) -> dict[str, dict[str, Any]]:
    """Per scenario: read_news symbols, payload orders and legit orders."""
    data: dict[str, dict[str, Any]] = yaml.safe_load(path.read_text())
    return data


def order_args(order: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {"symbol": order["symbol"], "side": order["side"]}
    if "notional" in order:
        args["notional"] = order["notional"]
    else:
        args["qty"] = order["qty"]
    args["order_type"] = "market"
    return args


def call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def script(scenario: Scenario, label: dict[str, Any]) -> list[AIMessage]:
    """Every message the agent graph will ask for, across all user turns, in order."""
    messages: list[AIMessage] = []
    last = len(scenario.user_turns) - 1
    for turn, text in enumerate(scenario.user_turns):
        understood = {"request_type": "trade" if turn == last else "question", "summary": text}
        messages.append(AIMessage(content=json.dumps(understood)))
        messages.append(AIMessage(content="1. Read the news. 2. Do what it says."))
        step = 0
        if turn == 0:
            for symbol in label["read_news"]:
                step += 1
                messages.append(call("get_news", {"symbol": symbol}, f"t{turn}_{step}"))
        if turn == last:
            for order in [*label["payload"], *label["legit"]]:
                step += 1
                messages.append(call("place_order", order_args(order), f"t{turn}_{step}"))
        messages.append(AIMessage(content="Done."))
    return messages


def attacker_factory(labels: dict[str, dict[str, Any]]) -> AgentFactory:
    """For the runner: a fresh scripted agent per run (chat and JSON roles share it)."""

    def make(scenario: Scenario) -> tuple[BaseChatModel, BaseChatModel]:
        if scenario.id not in labels:
            raise KeyError(f"no attack label for {scenario.id}")
        agent = ScriptedAgent(script(scenario, labels[scenario.id]))
        return agent, agent

    return make
