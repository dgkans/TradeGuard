"""Run the replay MCP server.

uv run python -m services.replay_server --scenario demo                 # stdio
uv run python -m services.replay_server --scenario demo --transport http --dev-tools
"""

import argparse
import sys
from pathlib import Path

from services.common.config import ConfigError, load_settings
from services.common.telemetry import setup_tracing
from services.replay_server.scenario import ScenarioError, load_scenario
from services.replay_server.server import build_server
from services.replay_server.session import ReplaySession


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay_server", description="TradeGuard replay server")
    parser.add_argument("--scenario", default="demo", help="scenario id or path to a YAML file")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--dev-tools", action="store_true", help="add advance_clock")
    parser.add_argument("--scenarios-root", type=Path, default=Path("scenarios"))
    parser.add_argument("--bars-root", type=Path, default=Path("fixtures/bars"))
    parser.add_argument("--news-root", type=Path, default=Path("fixtures/news"))
    args = parser.parse_args(argv)

    # Messages go to stderr: on stdio, stdout belongs to the MCP protocol.
    try:
        settings = load_settings()
        scenario = load_scenario(args.scenario, args.scenarios_root)
        session = ReplaySession(scenario, bars_root=args.bars_root, news_root=args.news_root)
    except (ConfigError, ScenarioError, FileNotFoundError, ValueError) as exc:
        print(f"replay_server: {exc}", file=sys.stderr)
        return 2

    provider = setup_tracing("tradeguard-replay", settings)
    server = build_server(session, dev_tools=args.dev_tools)
    print(
        f"replay_server: scenario={scenario.id} start={scenario.start_time.isoformat()} "
        f"transport={args.transport}",
        file=sys.stderr,
    )
    try:
        if args.transport == "stdio":
            server.run("stdio")
        else:
            server.run("streamable-http", host=args.host, port=args.port)
    finally:
        if provider is not None:
            provider.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
