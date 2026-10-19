"""Baseline agent (no guard): read only answers, orders, tool logging, limits, resume."""

import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from services.agent.graph import AgentContext, build_graph, run_turn
from services.agent.tools import McpToolset, ToolCallLog
from services.common.config import load_settings
from services.replay_server.server import build_server
from services.replay_server.session import ReplaySession
from tests.agent.conftest import ScriptedChatModel, answer, plan, tool_call, understand

CONTEXT = AgentContext(scenario_id="demo", watchlist=["NVDA", "AMD", "AAPL", "MSFT", "TSLA"])


async def run_once(
    session: ReplaySession,
    model: ScriptedChatModel,
    text: str,
    log: ToolCallLog,
    **graph_kwargs: Any,
) -> dict[str, Any]:
    async with McpToolset(build_server(session, dev_tools=True), log=log) as toolset:
        graph = build_graph(model, model, toolset.tools, context=CONTEXT, **graph_kwargs)
        return await run_turn(graph, text, thread_id=str(uuid.uuid4()))


async def test_read_only_question_uses_tools(replay_session: ReplaySession) -> None:
    model = ScriptedChatModel(
        [
            understand("read_only", "price of NVDA"),
            plan(),
            tool_call("get_quote", {"symbol": "NVDA"}),
            answer("NVDA last traded at $212.74."),
        ]
    )
    log = ToolCallLog()
    result = await run_once(replay_session, model, "What is NVDA trading at?", log)

    assert result["answer"] == "NVDA last traded at $212.74."
    assert result["request_type"] == "read_only"
    assert [r["tool"] for r in log.records] == ["get_quote"]
    # The model saw the real tool result before answering.
    tool_messages = [m for m in model.prompts[-1] if isinstance(m, ToolMessage)]
    assert json.loads(str(tool_messages[0].content))["symbol"] == "NVDA"
    assert model.remaining == 0


async def test_explicit_instruction_places_paper_order(replay_session: ReplaySession) -> None:
    model = ScriptedChatModel(
        [
            understand("trade", "buy $500 of AMD"),
            plan(),
            tool_call("place_order", {"symbol": "AMD", "side": "buy", "notional": 500}),
            answer("Placed a $500 market buy of AMD."),
        ]
    )
    await run_once(replay_session, model, "Buy $500 of AMD", ToolCallLog())
    orders = replay_session.list_orders()["orders"]
    assert len(orders) == 1
    assert orders[0]["symbol"] == "AMD"
    assert orders[0]["side"] == "buy"
    assert orders[0]["notional"] == 500.0


async def test_every_tool_call_is_logged_with_args_and_result(
    replay_session: ReplaySession, tmp_path: Path
) -> None:
    model = ScriptedChatModel(
        [
            understand("read_only", "account and bad symbol"),
            plan(),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_account", "args": {}, "id": "a"},
                    {"name": "get_quote", "args": {"symbol": "ZZZZ"}, "id": "b"},
                ],
            ),
            answer("Done."),
        ]
    )
    log = ToolCallLog(path=tmp_path / "run.jsonl", thread_id="t1")
    await run_once(replay_session, model, "Show my account and quote ZZZZ", log)

    lines = (tmp_path / "run.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines]
    by_tool = {r["tool"]: r for r in records}
    assert by_tool["get_account"]["args"] == {}
    assert by_tool["get_account"]["result"]["cash"] == 10000.0
    assert by_tool["get_account"]["error"] is None
    assert by_tool["get_quote"]["args"] == {"symbol": "ZZZZ"}
    assert "unknown symbol" in by_tool["get_quote"]["error"]
    for record in records:
        assert record["thread_id"] == "t1"
        assert record["latency_ms"] >= 0
        assert record["prompt_version"] == "system_v1"


async def test_dev_tools_are_hidden_from_the_agent(replay_session: ReplaySession) -> None:
    async with McpToolset(build_server(replay_session, dev_tools=True), log=ToolCallLog()) as ts:
        names = {tool.name for tool in ts.tools}
        assert "advance_clock" not in names
        assert "place_order" in names
        # The CLI can still drive the clock directly.
        clock = await ts.call("advance_clock", {"minutes": 5})
        assert clock["now"] == "2026-09-15T14:35:00+00:00"


async def test_tool_call_cap_stops_the_loop(replay_session: ReplaySession) -> None:
    looping = [tool_call("get_clock", {}, call_id=f"c{i}") for i in range(5)]
    model = ScriptedChatModel([understand("read_only", "loop"), plan(), *looping])
    log = ToolCallLog()
    result = await run_once(replay_session, model, "loop forever", log, max_tool_calls=3)
    assert len(log.records) == 3
    assert "tool call limit" in result["answer"]


async def test_unparseable_understand_output_falls_back(replay_session: ReplaySession) -> None:
    model = ScriptedChatModel([AIMessage(content="not json"), plan(), answer("Hi!")])
    result = await run_once(replay_session, model, "hello", ToolCallLog())
    assert result["request_type"] == "other"
    assert result["answer"] == "Hi!"


async def test_plan_reaches_the_acting_prompt(replay_session: ReplaySession) -> None:
    model = ScriptedChatModel(
        [understand("read_only", "x"), plan("1. Call get_clock."), answer("ok")]
    )
    await run_once(replay_session, model, "what time is it", ToolCallLog())
    act_prompt = model.prompts[2]
    assert "1. Call get_clock." in str(act_prompt[0].content)
    assert "NVDA" in str(act_prompt[0].content)  # watchlist in the system prompt


def postgres_available() -> bool:
    try:
        url = load_settings().database_url.get_secret_value()
        with psycopg.connect(url, connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.mark.skipif(not postgres_available(), reason="Postgres not reachable at DATABASE_URL")
async def test_session_resumes_from_postgres_checkpoint(replay_session: ReplaySession) -> None:
    """Turn 1 runs on one checkpointer connection and graph. Both are closed, as on process
    exit. Turn 2 builds new ones and must see turn 1's conversation."""
    url = load_settings().database_url.get_secret_value()
    thread_id = f"test-{uuid.uuid4()}"

    first = ScriptedChatModel(
        [understand("read_only", "NVDA"), plan(), tool_call("get_quote", {"symbol": "NVDA"}),
         answer("NVDA is $212.74.")]
    )  # fmt: skip
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        async with McpToolset(build_server(replay_session), log=ToolCallLog()) as ts:
            graph = build_graph(first, first, ts.tools, context=CONTEXT, checkpointer=saver)
            await run_turn(graph, "Price of NVDA?", thread_id=thread_id)

    second = ScriptedChatModel([understand("read_only", "AMD too"), plan(), answer("AMD...")])
    async with (
        AsyncPostgresSaver.from_conn_string(url) as saver,
        McpToolset(build_server(replay_session), log=ToolCallLog()) as ts,
    ):
        graph = build_graph(second, second, ts.tools, context=CONTEXT, checkpointer=saver)
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        restored = state.values["messages"]
        assert isinstance(restored[0], HumanMessage)
        assert restored[0].content == "Price of NVDA?"
        assert restored[-1].content == "NVDA is $212.74."
        await run_turn(graph, "And AMD?", thread_id=thread_id)

    act_prompt = second.prompts[2]
    contents = [str(m.content) for m in act_prompt]
    assert "Price of NVDA?" in contents
    assert "NVDA is $212.74." in contents
    assert contents[-1] == "And AMD?"


async def test_style_note_is_added_to_the_system_prompt_only_when_set(
    replay_session: ReplaySession,
) -> None:
    """Trader sessions (F10b) add a plain-language style note; evals and the CLI don't."""
    from langchain_core.messages import SystemMessage

    def acting_system(model: ScriptedChatModel) -> str:
        systems = [str(m.content) for p in model.prompts for m in p if isinstance(m, SystemMessage)]
        return [s for s in systems if "trading assistant" in s][-1]  # act runs after plan

    plain_model = ScriptedChatModel([understand("question", "x"), plan(), answer("ok")])
    styled_model = ScriptedChatModel([understand("question", "x"), plan(), answer("ok")])
    styled = AgentContext("desk", ["NVDA"], style="Never show order ids.")
    for model, context in ((plain_model, CONTEXT), (styled_model, styled)):
        log = ToolCallLog()
        async with McpToolset(build_server(replay_session), log=log) as toolset:
            graph = build_graph(model, model, toolset.tools, context=context)
            await run_turn(graph, "hi", thread_id=str(uuid.uuid4()))
    assert "Never show order ids." not in acting_system(plain_model)
    assert acting_system(styled_model).rstrip().endswith("Never show order ids.")
