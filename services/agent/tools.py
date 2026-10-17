"""Bind MCP tools to LangChain, and log every call.

We wrap the MCP SDK 2.x `Client` ourselves because `langchain-mcp-adapters` requires
`mcp<2` (DECISIONS.md D007). In F3 the target is the replay server; from F4 on it is the
gateway, with no change here.
"""

import json
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.lowlevel import Server
from mcp.server.mcpserver import MCPServer
from opentelemetry import trace

from services.agent.prompts import SYSTEM_PROMPT

# Operator controls that the model must never be able to call. Hiding them here is a
# convenience; the gateway refuses them without the operator token regardless.
DEV_ONLY_TOOLS = frozenset({"advance_clock", "resolve_approval"})

McpTarget = MCPServer | Server[Any] | StdioServerParameters | str
tracer = trace.get_tracer("tradeguard.agent")


class ToolCallLog:
    """Records every tool call: arguments, result or error, and latency.

    Records are kept in memory and, when `path` is set, appended to a JSON lines file.
    `echo` is called with each record, for live CLI output.
    """

    def __init__(
        self,
        path: Path | None = None,
        thread_id: str = "",
        prompt_version: str = SYSTEM_PROMPT,
        echo: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.path = path
        self.thread_id = thread_id
        self.prompt_version = prompt_version
        self.echo = echo
        self.records: list[dict[str, Any]] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self, tool: str, args: dict[str, Any], result: Any, error: str | None, latency_ms: float
    ) -> None:
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "thread_id": self.thread_id,
            "prompt_version": self.prompt_version,
            "tool": tool,
            "args": args,
            "result": result,
            "error": error,
            "latency_ms": round(latency_ms, 2),
        }
        self.records.append(entry)
        if self.path is not None:
            with self.path.open("a") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        if self.echo is not None:
            self.echo(entry)


class McpToolset:
    """Async context manager: connects to an MCP server and exposes its tools to LangChain.

    `tools` holds the agent visible tools (dev only tools removed). `call` invokes any tool
    directly, for operator actions such as advancing the sim clock.
    """

    def __init__(self, target: McpTarget, log: ToolCallLog) -> None:
        self.target = target
        self.log = log
        self.tools: list[BaseTool] = []
        self._client: Client | None = None
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> "McpToolset":
        self._client = await self._stack.enter_async_context(Client(self.target))
        listed = await self._client.list_tools()
        for spec in listed.tools:
            if spec.name in DEV_ONLY_TOOLS:
                continue
            self.tools.append(self._make_tool(spec.name, spec.description or "", spec.input_schema))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._stack.aclose()
        self._client = None

    async def call(
        self, name: str, args: dict[str, Any], meta: dict[str, Any] | None = None
    ) -> Any:
        """Operator call, not logged as an agent call. `meta` goes into the request `_meta`
        (for example the gateway operator token). Raises RuntimeError on tool error."""
        result, error = await self._raw_call(name, args, meta)
        if error is not None:
            raise RuntimeError(error)
        if isinstance(result, str) and result.lstrip().startswith("{"):
            return json.loads(result)  # gateway native tools reply with JSON text
        return result

    async def _raw_call(
        self, name: str, args: dict[str, Any], meta: dict[str, Any] | None = None
    ) -> tuple[Any, str | None]:
        if self._client is None:
            raise RuntimeError("toolset is not connected")
        response = await self._client.call_tool(name, args, meta=meta)  # type: ignore[arg-type]
        text_parts = []
        for block in response.content:
            text = getattr(block, "text", None)
            if text:
                text_parts.append(text)
        if response.is_error:
            return None, " ".join(text_parts) or "tool error"
        if response.structured_content is not None:
            return response.structured_content, None
        return " ".join(text_parts), None

    def _make_tool(self, name: str, description: str, schema: dict[str, Any]) -> BaseTool:
        async def invoke(**kwargs: Any) -> str:
            started = time.perf_counter()
            with tracer.start_as_current_span(
                "agent.tool_call", attributes={"tradeguard.tool": name}
            ) as span:
                try:
                    result, error = await self._raw_call(name, kwargs)
                except Exception as exc:  # transport failures reach the model as errors too
                    result, error = None, f"{type(exc).__name__}: {exc}"
                span.set_attribute("tradeguard.tool_error", error is not None)
            self.log.record(name, kwargs, result, error, (time.perf_counter() - started) * 1000)
            if error is not None:
                return f"ERROR: {error}"
            return json.dumps(result, default=str)

        return StructuredTool(
            name=name, description=description, args_schema=schema, coroutine=invoke
        )
