"""The replay server speaks MCP over in-process, stdio and streamable HTTP."""

import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from services.replay_server.scenario import load_scenario
from services.replay_server.server import build_server
from services.replay_server.session import ReplaySession

REPO_ROOT = Path(__file__).resolve().parents[2]

AGENT_TOOLS = {
    "get_bars",
    "get_quote",
    "get_news",
    "get_account",
    "get_positions",
    "place_order",
    "cancel_order",
    "list_orders",
    "get_clock",
    "detect_patterns",
    "scan_watchlist",
}


def demo_session() -> ReplaySession:
    return ReplaySession(
        load_scenario("demo", REPO_ROOT / "scenarios"),
        bars_root=REPO_ROOT / "fixtures/bars",
        news_root=REPO_ROOT / "fixtures/news",
    )


async def tool_names(client: Client) -> set[str]:
    result = await client.list_tools()
    names = set()
    for tool in result.tools:
        names.add(tool.name)
    return names


async def call(client: Client, name: str, args: dict[str, Any]) -> Any:
    result = await client.call_tool(name, args)
    assert not result.is_error, result.content
    return result.structured_content


async def test_in_process_tools_and_call() -> None:
    async with Client(build_server(demo_session())) as client:
        assert await tool_names(client) == AGENT_TOOLS
        quote = await call(client, "get_quote", {"symbol": "NVDA"})
        assert quote["symbol"] == "NVDA"
        assert quote["price"] > 0


async def test_dev_tools_add_clock_control() -> None:
    async with Client(build_server(demo_session(), dev_tools=True)) as client:
        assert await tool_names(client) == AGENT_TOOLS | {"advance_clock"}
        before = await call(client, "get_clock", {})
        after = await call(client, "advance_clock", {"minutes": 5})
        assert after["now"] > before["now"]


async def test_bad_arguments_return_tool_error() -> None:
    async with Client(build_server(demo_session())) as client:
        result = await client.call_tool("get_quote", {"symbol": "ZZZZ"})
        assert result.is_error


async def test_stdio_transport() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "services.replay_server", "--scenario", "demo"],
        cwd=REPO_ROOT,
    )
    async with Client(params) as client:
        assert await tool_names(client) == AGENT_TOOLS
        account = await call(client, "get_account", {})
        assert account["cash"] > 0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def wait_for_port(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    pytest.fail(f"server did not listen on {port} within {timeout}s")


async def test_streamable_http_transport() -> None:
    port = free_port()
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable, "-m", "services.replay_server", "--scenario", "demo",
            "--transport", "http", "--port", str(port),
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        wait_for_port(port, timeout=20)
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            assert await tool_names(client) == AGENT_TOOLS
            clock = await call(client, "get_clock", {})
            assert clock["now"].startswith("2026-")
    finally:
        process.terminate()
        process.wait(timeout=10)
