"""`tradeguard chat`: talk to the baseline agent inside a replay scenario.

    uv run tradeguard chat --scenario demo
    uv run tradeguard chat --scenario demo --session my-session      # resume later
    uv run tradeguard chat --scenario demo --once "What is NVDA at?"
    uv run tradeguard chat --scenario demo --direct     # no gateway (no_guard baseline)
    uv run tradeguard gateway-stats --session my-session

In chat, `/advance N` moves the simulated clock N minutes (the agent itself cannot),
`/clock` shows it, and `/quit` exits.
"""

import argparse
import asyncio
import json
import secrets
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from mcp.client.stdio import StdioServerParameters

from services.agent.approvals import wait_for_decision
from services.agent.graph import AgentContext, build_graph, resume_turn, run_turn
from services.agent.llm import make_chat_model
from services.agent.provenance import report_user_message
from services.agent.tools import McpToolset, ToolCallLog
from services.common.config import ConfigError, Settings, child_env, load_settings
from services.common.telemetry import setup_tracing
from services.gateway.mcp_proxy.proxy import OPERATOR_META_KEY, OPERATOR_TOKEN_ENV, percentile
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
    operator_token = secrets.token_urlsafe(24)
    if args.direct:
        server_args = ["-m", "services.replay_server", "--scenario", args.scenario, "--dev-tools"]
    else:
        server_args = ["-m", "services.gateway", "--session", session_id, "--scenario",
                       args.scenario, "--dev-tools", "--policy", args.policy]  # fmt: skip
    env = child_env() | {OPERATOR_TOKEN_ENV: operator_token}
    server = StdioServerParameters(
        command=sys.executable, args=server_args, cwd=Path.cwd(), env=env
    )
    operator_meta = {OPERATOR_META_KEY: operator_token}
    context = AgentContext(scenario_id=scenario.id, watchlist=scenario.watchlist)
    chat_model = make_chat_model(settings)
    json_model = make_chat_model(settings, json_mode=True)
    url = settings.database_url.get_secret_value()

    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        async with McpToolset(server, log=log) as toolset:
            graph = build_graph(chat_model, json_model, toolset.tools, context=context,
                                checkpointer=saver, max_tool_calls=args.max_tool_calls)  # fmt: skip
            route = "direct (no guard)" if args.direct else f"via gateway, policy {args.policy}"
            model = f"{settings.llm_provider}:{settings.llm_model}"
            print(f"session {session_id} | scenario {scenario.id} | model {model} | {route} "
                  "| paper/replay only")  # fmt: skip
            print(f"tool log: {log.path}")
            turn = Turn(graph, toolset, session_id, operator_meta, settings,
                        guarded=not args.direct)  # fmt: skip
            if args.once:
                print(f"\nyou> {args.once}")
                await turn.send(args.once)
                return 0
            return await repl(turn)


class Turn:
    """Runs one user message, including any pause for human approval."""

    def __init__(
        self,
        graph: Any,
        toolset: McpToolset,
        session_id: str,
        operator_meta: dict[str, Any],
        settings: Settings,
        guarded: bool,
    ) -> None:
        self.guarded = guarded
        self.graph = graph
        self.toolset = toolset
        self.session_id = session_id
        self.operator_meta = operator_meta
        self.settings = settings

    async def send(self, text: str) -> None:
        if self.guarded:
            # Trusted provenance for the taint rule: sent by this code, not by the model.
            await report_user_message(self.toolset, text, self.operator_meta)
        result = await run_turn(self.graph, text, thread_id=self.session_id)
        print(f"[understand: {result['request_type']} | {result['summary']}]")
        while result["pending_approval"]:
            outcome = await self.wait(result["pending_approval"])
            result = await resume_turn(self.graph, outcome, thread_id=self.session_id)
        print(f"agent> {result['answer']}")

    async def wait(self, pending: dict[str, Any]) -> dict[str, Any]:
        approval_id = pending["approval_id"]
        base = f"http://{self.settings.api_host}:{self.settings.api_port}/approvals/{approval_id}"
        print(f"\n[approval needed] {pending['tool']} {json.dumps(pending['args'])}")
        print(f"  why: {pending['reason']} ({pending['rule_id']})")
        print(f"  expires: {pending['expires_at']}")
        print(f"  approve: curl -s -X POST {base}/approve -H 'content-type: application/json' "
              f"-d '{{\"decided_by\": \"you\", \"note\": \"ok\"}}'")  # fmt: skip
        print(f"  reject:  curl -s -X POST {base}/reject -H 'content-type: application/json' "
              f"-d '{{\"decided_by\": \"you\", \"note\": \"why\"}}'")  # fmt: skip
        print("  waiting for a decision...", flush=True)
        outcome = await wait_for_decision(
            self.toolset, approval_id, self.operator_meta,
            max_wait_seconds=self.settings.approval_timeout_seconds + 30,
        )  # fmt: skip
        print(f"[approval {outcome['status']}] decided_by={outcome.get('decided_by')} "
              f"note={outcome.get('note')!r} executed={outcome.get('executed')}")  # fmt: skip
        return outcome


async def repl(turn: "Turn") -> int:
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
            print(await turn.toolset.call("get_clock", {}, turn.operator_meta))
            continue
        if text.startswith("/advance"):
            parts = text.split()
            minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 5
            print(await turn.toolset.call("advance_clock", {"minutes": minutes},
                                          turn.operator_meta))  # fmt: skip
            continue
        await turn.send(text)


STATS_QUERY = """
select tool, decision, overhead_ms, upstream_ms from audit_events
where session_id = %s and latency_ms is not null order by id
"""


def gateway_stats(args: argparse.Namespace, settings: Settings) -> int:
    """Print per call gateway overhead (p50, p95) for one session from audit_events."""
    with psycopg.connect(settings.database_url.get_secret_value()) as conn:
        rows = conn.execute(STATS_QUERY, (args.session,)).fetchall()
    if not rows:
        print(f"no completed audit events for session {args.session}")
        return 1
    overheads = []
    upstreams = []
    decisions: dict[str, int] = {}
    for _tool, decision, overhead, upstream in rows:
        overheads.append(float(overhead))
        upstreams.append(float(upstream))
        decisions[decision] = decisions.get(decision, 0) + 1
    print(f"session {args.session}: {len(rows)} calls, decisions {decisions}")
    print(f"gateway overhead  p50 {percentile(overheads, 50):.3f} ms  "
          f"p95 {percentile(overheads, 95):.3f} ms")  # fmt: skip
    print(f"upstream time     p50 {percentile(upstreams, 50):.3f} ms  "
          f"p95 {percentile(upstreams, 95):.3f} ms")  # fmt: skip
    return 0


def run_api(settings: Settings) -> int:
    """Serve the dashboard API (approvals, audit, policies, evals, chat) on API_HOST:API_PORT."""
    import uvicorn

    from services.api.app import create_postgres_app

    app = create_postgres_app(settings)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradeguard")
    commands = parser.add_subparsers(dest="command", required=True)
    chat_cmd = commands.add_parser("chat", help="chat with the baseline agent")
    chat_cmd.add_argument("--scenario", default="demo")
    chat_cmd.add_argument("--session", help="session id to create or resume")
    chat_cmd.add_argument("--once", help="send one message and exit")
    chat_cmd.add_argument("--max-tool-calls", type=int, default=20)
    chat_cmd.add_argument(
        "--direct", action="store_true", help="skip the gateway (no_guard baseline)"
    )
    chat_cmd.add_argument("--policy", default="policies/default.yaml", help="gateway policy")
    stats_cmd = commands.add_parser("gateway-stats", help="gateway overhead for a session")
    stats_cmd.add_argument("--session", required=True)
    commands.add_parser("api", help="run the dashboard API (APPROVAL_* and API_* settings)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        if args.command == "gateway-stats":
            return gateway_stats(args, settings)
        if args.command == "api":
            return run_api(settings)
        provider = setup_tracing("tradeguard-agent", settings)
        try:
            return asyncio.run(chat(args, settings))
        finally:
            if provider is not None:
                provider.shutdown()
    except (ConfigError, ScenarioError) as exc:
        print(f"tradeguard: {exc}", file=sys.stderr)
        return 2
    except psycopg.OperationalError as exc:
        print(f"tradeguard: cannot reach Postgres at DATABASE_URL ({exc})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
