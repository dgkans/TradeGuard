"""Scripted chat model and an in-process replay server for agent tests."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from services.replay_server.scenario import load_scenario
from services.replay_server.session import ReplaySession

REPO_ROOT = Path(__file__).resolve().parents[2]


class ScriptedChatModel(BaseChatModel):
    """Returns pre written responses in order and records every prompt it was given."""

    _responses: list[AIMessage] = PrivateAttr(default_factory=list)
    _prompts: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: Sequence[AIMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def prompts(self) -> list[list[BaseMessage]]:
        return self._prompts

    @property
    def remaining(self) -> int:
        return len(self._responses)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._prompts.append(list(messages))
        if not self._responses:
            raise AssertionError("scripted model ran out of responses")
        return ChatResult(generations=[ChatGeneration(message=self._responses.pop(0))])


def understand(request_type: str, summary: str) -> AIMessage:
    return AIMessage(content=f'{{"request_type": "{request_type}", "summary": "{summary}"}}')


def plan(text: str = "1. Use tools. 2. Answer.") -> AIMessage:
    return AIMessage(content=text)


def tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def answer(text: str) -> AIMessage:
    return AIMessage(content=text)


@pytest.fixture
def replay_session() -> ReplaySession:
    return ReplaySession(
        load_scenario("demo", REPO_ROOT / "scenarios"),
        bars_root=REPO_ROOT / "fixtures/bars",
        news_root=REPO_ROOT / "fixtures/news",
    )
