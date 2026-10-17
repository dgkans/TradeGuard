"""Recorded LLM responses ("tapes") for repeatable eval runs.

One tape per run (`<scenario>__<config>__r<repeat>.json`), with one stream per role
(`agent`, `judge`). A response is looked up by its position in the stream, not by
hashing the prompt: prompts contain wall clock values (approval expiry), but the call order
is fixed for a given run. Modes:
- live: call the real model, count tokens, store nothing
- record: call the real model and save every response
- replay: never call a model; return the saved responses in order
Replaying a recorded run reproduces its tool calls and therefore its metrics.
"""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import PrivateAttr

Mode = Literal["live", "record", "replay"]


class TapeExhaustedError(Exception):
    """Replay asked for more responses than the tape holds (the run diverged)."""


class Tape:
    def __init__(self, path: Path, mode: Mode) -> None:
        self.path = path
        self.mode = mode
        self.streams: dict[str, list[dict[str, Any]]] = {}
        self.positions: dict[str, int] = {}
        self.tokens: dict[str, int] = {}
        self.calls: dict[str, int] = {}
        if mode == "replay":
            if not path.is_file():
                raise TapeExhaustedError(f"no tape recorded at {path}")
            self.streams = json.loads(path.read_text())["streams"]

    def next(self, stream: str) -> dict[str, Any]:
        entries = self.streams.get(stream, [])
        position = self.positions.get(stream, 0)
        if position >= len(entries):
            raise TapeExhaustedError(f"tape {self.path.name} has no {stream} response #{position}")
        self.positions[stream] = position + 1
        return entries[position]

    def add(self, stream: str, entry: dict[str, Any]) -> None:
        self.streams.setdefault(stream, []).append(entry)
        usage = entry.get("usage_metadata") or {}
        self.tokens[stream] = self.tokens.get(stream, 0) + int(usage.get("total_tokens", 0))
        self.calls[stream] = self.calls.get(stream, 0) + 1

    def save(self) -> None:
        if self.mode != "record":
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        run_key = self.path.stem
        self.path.write_text(json.dumps({"run_key": run_key, "streams": self.streams}, indent=1))


def to_entry(message: AIMessage) -> dict[str, Any]:
    calls = [{"name": c["name"], "args": c["args"], "id": c["id"]} for c in message.tool_calls]
    usage = dict(message.usage_metadata) if message.usage_metadata else {}
    content = message.content if isinstance(message.content, str) else json.dumps(message.content)
    return {"content": content, "tool_calls": calls, "usage_metadata": usage}


def from_entry(entry: dict[str, Any]) -> AIMessage:
    usage = entry.get("usage_metadata") or None
    return AIMessage(
        content=entry.get("content", ""),
        tool_calls=entry.get("tool_calls", []),
        usage_metadata=usage,
    )


class TapeChatModel(BaseChatModel):
    """Chat model that records to, or replays from, one stream of a tape."""

    _tape: Tape = PrivateAttr()
    _stream_name: str = PrivateAttr()
    _inner: Runnable[Any, Any] | None = PrivateAttr(default=None)

    def __init__(self, tape: Tape, stream: str, inner: Runnable[Any, Any] | None, **kw: Any):
        super().__init__(**kw)
        self._tape = tape
        self._stream_name = stream
        self._inner = inner

    @property
    def _llm_type(self) -> str:
        return "tape"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "TapeChatModel":
        inner = self._inner
        if inner is not None and isinstance(inner, BaseChatModel):
            inner = inner.bind_tools(tools, **kwargs)
        return TapeChatModel(self._tape, self._stream_name, inner)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("TapeChatModel is async only")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self._tape.mode == "replay":
            entry = self._tape.next(self._stream_name)
            self._count(entry)
            return ChatResult(generations=[ChatGeneration(message=from_entry(entry))])
        if self._inner is None:
            raise RuntimeError("live or record mode needs a real model")
        reply = await self._inner.ainvoke(messages)
        entry = to_entry(reply)
        if self._tape.mode == "record":
            self._tape.add(self._stream_name, entry)
        else:
            self._count(entry)
        return ChatResult(generations=[ChatGeneration(message=from_entry(entry))])

    def _count(self, entry: dict[str, Any]) -> None:
        usage = entry.get("usage_metadata") or {}
        tape = self._tape
        tape.tokens[self._stream_name] = tape.tokens.get(self._stream_name, 0) + int(
            usage.get("total_tokens", 0)
        )
        tape.calls[self._stream_name] = tape.calls.get(self._stream_name, 0) + 1


ModelMaker = Callable[[str], BaseChatModel]


class TapeFactory:
    """Builds the agent and judge models for each run, wired to that run's tape.

    `make_model(role)` returns a real chat model for a role (`agent`, `agent_json`,
    `judge`); it's only used in live and record modes.
    """

    def __init__(self, mode: Mode, root: Path, make_model: ModelMaker | None = None) -> None:
        if mode != "replay" and make_model is None:
            raise ValueError(f"{mode} mode needs make_model")
        self.mode = mode
        self.root = root
        self.make_model = make_model
        self._real: dict[str, BaseChatModel] = {}

    def real(self, role: str) -> BaseChatModel | None:
        if self.make_model is None:
            return None
        if role not in self._real:
            self._real[role] = self.make_model(role)
        return self._real[role]

    def tape(self, run_key: str) -> Tape:
        return Tape(self.root / f"{run_key}.json", self.mode)

    def models(self, tape: Tape) -> tuple[TapeChatModel, TapeChatModel, TapeChatModel]:
        """(agent chat, agent JSON mode, judge). Agent chat and JSON share one stream: the
        graph calls them in a fixed order."""
        return (
            TapeChatModel(tape, "agent", self.real("agent")),
            TapeChatModel(tape, "agent", self.real("agent_json")),
            TapeChatModel(tape, "judge", self.real("judge")),
        )
