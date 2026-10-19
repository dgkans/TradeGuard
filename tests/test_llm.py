"""The chat model factory (PLAN 15.2): Ollama locally, Groq's free plan in production (F12).

No network: Groq requests go to an httpx mock that records them."""

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from services.common.config import ConfigError, load_settings
from services.common.llm import make_chat_model


def groq_settings(**extra: str) -> Any:
    env = {"LLM_PROVIDER": "groq", "LLM_MODEL": "openai/gpt-oss-120b",
           "GROQ_API_KEY": "gsk_test_not_a_real_key", **extra}  # fmt: skip
    return load_settings(env_file=None, environ=env)


def recording_client(sent: list[dict[str, Any]], reply: dict[str, Any]) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        body = {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "message": reply, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}}  # fmt: skip
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def with_client(model: Any, client: httpx.AsyncClient) -> Any:
    """The same model, sending its requests to `client`."""
    fields = model.model_dump(exclude={"async_client", "client"})
    return type(model)(**{**fields, "api_key": "gsk_test_not_a_real_key",
                          "http_async_client": client})  # fmt: skip


@tool
def get_quote(symbol: str) -> str:
    """Latest price for a symbol."""
    return symbol


async def test_groq_sends_tools_with_short_hidden_reasoning() -> None:
    sent: list[dict[str, Any]] = []
    reply = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": "get_quote", "arguments": '{"symbol": "NVDA"}'}}]}  # fmt: skip
    model = with_client(make_chat_model(groq_settings()), recording_client(sent, reply))
    result = await model.bind_tools([get_quote]).ainvoke([HumanMessage("What is NVDA at?")])
    request = sent[-1]
    assert request["model"] == "openai/gpt-oss-120b"
    assert request["temperature"] < 1e-6  # langchain-groq sends 0 as 1e-8 (Groq quirk)
    assert request["reasoning_effort"] == "low"
    assert request["include_reasoning"] is False
    assert request["tools"][0]["function"]["name"] == "get_quote"
    assert result.tool_calls[0]["args"] == {"symbol": "NVDA"}


async def test_groq_json_mode_asks_for_a_json_object() -> None:
    sent: list[dict[str, Any]] = []
    reply = {"role": "assistant", "content": '{"kind": "question"}'}
    model = make_chat_model(groq_settings(), json_mode=True, model="openai/gpt-oss-20b")
    result = await with_client(model, recording_client(sent, reply)).ainvoke(
        [HumanMessage("classify")]
    )
    assert sent[-1]["model"] == "openai/gpt-oss-20b"
    assert sent[-1]["response_format"] == {"type": "json_object"}
    assert json.loads(str(result.content)) == {"kind": "question"}


def test_groq_needs_a_key_and_never_shows_it() -> None:
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        make_chat_model(load_settings(env_file=None, environ={"LLM_PROVIDER": "groq"}))
    model = make_chat_model(groq_settings())
    assert "gsk_test_not_a_real_key" not in repr(model)


def test_providers_not_wired_up_are_refused() -> None:
    settings = load_settings(env_file=None, environ={"LLM_PROVIDER": "gemini"})
    with pytest.raises(ConfigError, match="not wired up"):
        make_chat_model(settings)
