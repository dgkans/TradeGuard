"""Trading agent: understand -> plan -> act (tool loop) -> respond.

Talks to its tools over MCP: directly (the `no_guard` baseline) or through the gateway.
When the gateway escalates a call, the graph pauses in `await_approval` with a LangGraph
interrupt and resumes with the human decision's outcome.
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
from langgraph.types import Command, interrupt
from opentelemetry import trace

from services.agent.prompts import PLAN_PROMPT, SYSTEM_PROMPT, UNDERSTAND_PROMPT, load_prompt

DEFAULT_MAX_TOOL_CALLS = 20
REQUEST_TYPES = ("read_only", "trade", "other")
LIMIT_MESSAGE = (
    "I stopped because I reached the tool call limit for one request ({limit}). "
    "Please narrow the request and try again."
)
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
tracer = trace.get_tracer("tradeguard.agent")


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

    def route_after_tools(state: AgentState) -> Literal["await_approval", "act"]:
        if pending_escalation(state["messages"]) is not None:
            return "await_approval"
        return "act"

    async def await_approval(state: AgentState) -> dict[str, Any]:
        """Pause until a human decides, then replace the escalation with the outcome."""
        message = pending_escalation(state["messages"])
        if message is None:
            return {}
        parked = escalation_body(message) or {}
        payload = {
            "approval_id": parked.get("approval_id"),
            "tool": parked.get("tool"),
            "rule_id": parked.get("rule_id"),
            "reason": parked.get("reason"),
            "expires_at": parked.get("expires_at"),
            "args": tool_call_args(state["messages"], message.tool_call_id),
        }
        outcome = interrupt(payload)
        # Same id: the messages reducer replaces the escalation in place, so the model sees
        # the call's real final result.
        updated = ToolMessage(
            id=message.id,
            tool_call_id=message.tool_call_id,
            name=message.name,
            content=json.dumps(outcome, default=str),
        )
        return {"messages": [updated]}

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
    graph.add_node("await_approval", await_approval)
    graph.add_node("respond", respond)
    graph.add_edge(START, "understand")
    graph.add_edge("understand", "plan")
    graph.add_edge("plan", "act")
    graph.add_conditional_edges("act", route_after_act, ["tools", "respond"])
    graph.add_conditional_edges("tools", route_after_tools, ["await_approval", "act"])
    graph.add_conditional_edges("await_approval", route_after_tools, ["await_approval", "act"])
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=checkpointer)


def turn_config(thread_id: str, max_tool_calls: int) -> RunnableConfig:
    return {
        "configurable": {"thread_id": thread_id},
        # Each tool round is two or three steps (act, tools, approval); leave headroom.
        "recursion_limit": 3 * max_tool_calls + 10,
    }


async def run_turn(
    graph: Runnable[Any, Any], text: str, *, thread_id: str, max_tool_calls: int = 50
) -> dict[str, Any]:
    """Send one user message.

    Returns the answer plus the understand and plan outputs. If the gateway escalated a
    call, `answer` is None and `pending_approval` holds the interrupt payload: wait for the
    decision, then call `resume_turn`. Pausing needs a checkpointer.
    """
    with tracer.start_as_current_span(
        "agent.turn", attributes={"tradeguard.session_id": thread_id}
    ) as span:
        state = await graph.ainvoke(
            {"messages": [HumanMessage(text)]}, config=turn_config(thread_id, max_tool_calls)
        )
        span.set_attribute("tradeguard.request_type", state["request_type"])
    return turn_result(state)


async def resume_turn(
    graph: Runnable[Any, Any],
    outcome: dict[str, Any],
    *,
    thread_id: str,
    max_tool_calls: int = 50,
) -> dict[str, Any]:
    """Resume a paused turn with the approval outcome from the gateway."""
    with tracer.start_as_current_span(
        "agent.resume", attributes={"tradeguard.session_id": thread_id,
                                    "tradeguard.approval_status": str(outcome.get("status"))}
    ):  # fmt: skip
        state = await graph.ainvoke(
            Command(resume=outcome), config=turn_config(thread_id, max_tool_calls)
        )
    return turn_result(state)


def turn_result(state: dict[str, Any]) -> dict[str, Any]:
    interrupts = state.get("__interrupt__") or []
    pending = interrupts[0].value if interrupts else None
    return {
        "answer": None if pending else clean(text_of(state["messages"][-1])),
        "pending_approval": pending,
        "request_type": state.get("request_type"),
        "summary": state.get("summary"),
        "plan": state.get("plan"),
    }


def escalation_body(message: BaseMessage) -> dict[str, Any] | None:
    """The gateway's ESCALATE body if this tool message carries a pending approval."""
    if not isinstance(message, ToolMessage):
        return None
    raw = text_of(message).removeprefix("ERROR: ").strip()
    if not raw.startswith("{"):
        return None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    if (
        body.get("decision") == "ESCALATE"
        and body.get("approval_id")
        and (body.get("status") == "pending")
    ):
        return body
    return None


def pending_escalation(messages: Sequence[BaseMessage]) -> ToolMessage | None:
    """First pending escalation among the tool results after the last model message."""
    tail: list[ToolMessage] = []
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            tail.append(message)
        else:
            break
    for message in reversed(tail):
        if escalation_body(message) is not None:
            return message
    return None


def tool_call_args(messages: Sequence[BaseMessage], call_id: str) -> dict[str, Any]:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call["id"] == call_id:
                    return dict(call["args"])
    return {}


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
