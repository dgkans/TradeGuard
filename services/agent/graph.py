"""Baseline trading agent: understand -> plan -> act (tool loop) -> respond.

This is the `no_guard` configuration: tools go straight to the upstream MCP server.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from services.agent.prompts import PLAN_PROMPT, SYSTEM_PROMPT, UNDERSTAND_PROMPT, load_prompt

DEFAULT_MAX_TOOL_CALLS = 20
REQUEST_TYPES = ("read_only", "trade", "other")
LIMIT_MESSAGE = (
    "I stopped because I reached the tool call limit for one request ({limit}). "
    "Please narrow the request and try again."
)
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass(frozen=True)
class AgentContext:
    """Facts about the session that go into the system prompt."""

    scenario_id: str
    watchlist: list[str]


class AgentState(MessagesState):
    request_type: str
    summary: str
    plan: str
    prompt_version: str
    tool_calls_this_turn: int


def build_graph(
    chat: BaseChatModel,
    json_chat: BaseChatModel,
    tools: list[BaseTool],
    *,
    context: AgentContext,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """Compile the agent graph.

    `chat` acts and plans, `json_chat` (the same model in JSON mode) classifies the request.
    With a checkpointer, state persists per `thread_id` and a session can resume.
    """
    acting_model = chat.bind_tools(tools)
    tool_names = []
    for tool in tools:
        tool_names.append(f"- {tool.name}: {first_line(tool.description)}")
    tool_list = "\n".join(tool_names)

    async def understand(state: AgentState) -> dict[str, Any]:
        user_text = latest_user_text(state["messages"])
        reply = await json_chat.ainvoke(
            [SystemMessage(load_prompt(UNDERSTAND_PROMPT)), HumanMessage(user_text)]
        )
        request_type, summary = parse_understanding(text_of(reply), user_text)
        return {
            "request_type": request_type,
            "summary": summary,
            "prompt_version": SYSTEM_PROMPT,
            "tool_calls_this_turn": 0,
        }

    async def plan(state: AgentState) -> dict[str, Any]:
        prompt = load_prompt(PLAN_PROMPT).format(tools=tool_list)
        request = (
            f"Request type: {state['request_type']}\nSummary: {state['summary']}\n"
            f"User message: {latest_user_text(state['messages'])}"
        )
        reply = await chat.ainvoke([SystemMessage(prompt), HumanMessage(request)])
        return {"plan": clean(text_of(reply))}

    async def act(state: AgentState) -> dict[str, Any]:
        system = load_prompt(SYSTEM_PROMPT).format(
            scenario_id=context.scenario_id,
            watchlist=", ".join(context.watchlist),
            plan=state.get("plan", ""),
        )
        reply = await acting_model.ainvoke([SystemMessage(system), *state["messages"]])
        calls = len(reply.tool_calls) if isinstance(reply, AIMessage) else 0
        return {
            "messages": [reply],
            "tool_calls_this_turn": state.get("tool_calls_this_turn", 0) + calls,
        }

    def route_after_act(state: AgentState) -> Literal["tools", "respond"]:
        last = state["messages"][-1]
        wants_tools = isinstance(last, AIMessage) and bool(last.tool_calls)
        if wants_tools and state["tool_calls_this_turn"] <= max_tool_calls:
            return "tools"
        return "respond"

    async def respond(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            # Over the cap: answer the pending calls so the history stays valid for the
            # next turn, then stop.
            closing: list[BaseMessage] = []
            for call in last.tool_calls:
                closing.append(
                    ToolMessage(
                        content="not executed: tool call limit reached",
                        tool_call_id=call["id"] or "",
                    )
                )
            closing.append(AIMessage(content=LIMIT_MESSAGE.format(limit=max_tool_calls)))
            return {"messages": closing}
        if isinstance(last, AIMessage) and clean(text_of(last)):
            return {}
        # Empty final message: ask once more without tools for a plain answer.
        reply = await chat.ainvoke(
            [
                SystemMessage("Write the final answer to the user from the conversation."),
                *state["messages"],
            ]
        )
        return {"messages": [AIMessage(content=clean(text_of(reply)))]}

    graph = StateGraph(AgentState)
    graph.add_node("understand", understand)
    graph.add_node("plan", plan)
    graph.add_node("act", act)
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("respond", respond)
    graph.add_edge(START, "understand")
    graph.add_edge("understand", "plan")
    graph.add_edge("plan", "act")
    graph.add_conditional_edges("act", route_after_act, ["tools", "respond"])
    graph.add_edge("tools", "act")
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=checkpointer)


async def run_turn(
    graph: Runnable[Any, Any], text: str, *, thread_id: str, max_tool_calls: int = 50
) -> dict[str, Any]:
    """Send one user message and return the answer plus the understand and plan outputs."""
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id},
        # Each tool round is two steps (act, tools); allow headroom beyond the call cap.
        "recursion_limit": 2 * max_tool_calls + 10,
    }
    state = await graph.ainvoke({"messages": [HumanMessage(text)]}, config=config)
    return {
        "answer": clean(text_of(state["messages"][-1])),
        "request_type": state["request_type"],
        "summary": state["summary"],
        "plan": state["plan"],
    }


def parse_understanding(raw: str, fallback_summary: str) -> tuple[str, str]:
    """Pull request_type and summary out of the model's JSON. Falls back to 'other'."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            request_type = str(data.get("request_type", "other"))
            summary = str(data.get("summary") or fallback_summary)
            if request_type in REQUEST_TYPES:
                return request_type, summary
            return "other", summary
    return "other", fallback_summary


def latest_user_text(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return text_of(message)
    return ""


def text_of(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text", "")))
        elif isinstance(part, str):
            parts.append(part)
    return "".join(parts)


def clean(text: str) -> str:
    """Drop any <think> block a reasoning model leaks into its answer."""
    return THINK_BLOCK.sub("", text).strip()


def first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""
