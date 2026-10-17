"""`tradeguard chat`: talk to the baseline agent inside a replay scenario.

    uv run tradeguard chat --scenario demo
    uv run tradeguard chat --scenario demo --session my-session      # resume later
    uv run tradeguard chat --scenario demo --once "What is NVDA at?"

In chat, `/advance N` moves the simulated clock N minutes (the agent itself cannot),
`/clock` shows it, and `/quit` exits.
"""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from mcp.client.stdio import StdioServerParameters

from services.agent.graph import AgentContext, build_graph, run_turn
from services.agent.llm import make_chat_model
from services.agent.tools import McpToolset, ToolCallLog
from services.common.config import ConfigError, Settings, load_settings
from services.replay_server.scenario import ScenarioError, load_scenario

RUNS_DIR = Path("runs")


def echo_tool_call(record: dict[str, Any]) -> None:
    status = "error: " + str(record["error"]) if record["error"] else "ok"
    args = json.dumps(record["args"])
    print(f"  · {record['tool']}({args}) -> {status} [{record['latency_ms']:.0f} ms]")


async def chat(args: argparse.Namespace, settings: Settings) -> int:
    scenario = load_scenario(args.scenario)
    session_id = args.session or f"{scenario.id}-{uuid.uuid4().hex[:8]}"
    log = ToolCallLog(path=RUNS_DIR / f"{session_id}.jsonl", thread_id=session_id,
                      echo=echo_tool_call)  # fmt: skip
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "services.replay_server", "--scenario", args.scenario, "--dev-tools"],
        cwd=Path.cwd(),
    )
    context = AgentContext(scenario_id=scenario.id, watchlist=scenario.watchlist)
    chat_model = make_chat_model(settings)
    json_model = make_chat_model(settings, json_mode=True)
    url = settings.database_url.get_secret_value()

    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        async with McpToolset(server, log=log) as toolset:
            graph = build_graph(chat_model, json_model, toolset.tools, context=context,
                                checkpointer=saver, max_tool_calls=args.max_tool_calls)  # fmt: skip
            print(f"session {session_id} | scenario {scenario.id} | model "
                  f"{settings.llm_provider}:{settings.llm_model} | paper/replay only")  # fmt: skip
            print(f"tool log: {log.path}")
            if args.once:
                return await one_turn(graph, args.once, session_id)
            return await repl(graph, toolset, session_id)


async def one_turn(graph: Any, text: str, session_id: str) -> int:
    print(f"\nyou> {text}")
    result = await run_turn(graph, text, thread_id=session_id)
    print(f"[understand: {result['request_type']} | {result['summary']}]")
    print(f"agent> {result['answer']}\n")
    return 0


async def repl(graph: Any, toolset: McpToolset, session_id: str) -> int:
    print("Type a message. /advance N moves the clock, /clock shows it, /quit exits.")
    while True:
        try:
            text = (await asyncio.to_thread(input, "\nyou> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in ("/quit", "/exit"):
            return 0
        if text == "/clock":
            print(await toolset.call("get_clock", {}))
            continue
        if text.startswith("/advance"):
            parts = text.split()
            minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 5
            print(await toolset.call("advance_clock", {"minutes": minutes}))
            continue
        result = await run_turn(graph, text, thread_id=session_id)
        print(f"[understand: {result['request_type']} | {result['summary']}]")
        print(f"agent> {result['answer']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradeguard")
    commands = parser.add_subparsers(dest="command", required=True)
    chat_cmd = commands.add_parser("chat", help="chat with the baseline agent")
    chat_cmd.add_argument("--scenario", default="demo")
    chat_cmd.add_argument("--session", help="session id to create or resume")
    chat_cmd.add_argument("--once", help="send one message and exit")
    chat_cmd.add_argument("--max-tool-calls", type=int, default=20)
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        return asyncio.run(chat(args, settings))
    except (ConfigError, ScenarioError) as exc:
        print(f"tradeguard: {exc}", file=sys.stderr)
        return 2
    except psycopg.OperationalError as exc:
        print(f"tradeguard: cannot reach Postgres at DATABASE_URL ({exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
