"""Run the gateway for one agent session over stdio, in front of the replay server.

    uv run python -m services.gateway --session s1 --scenario demo

Audit events go to Postgres (DATABASE_URL). `--audit memory` is for tests only. Overhead
p50 and p95 are logged to stderr on exit.
"""

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp.client.stdio import StdioServerParameters
from mcp.server.stdio import stdio_server

from services.common.config import ConfigError, child_env, load_settings
from services.common.telemetry import setup_tracing
from services.gateway.approvals import ApprovalStore, MemoryApprovalStore, PostgresApprovalStore
from services.gateway.audit import AuditSink, MemoryAuditSink, PostgresAuditSink, check_schema
from services.gateway.mcp_proxy.proxy import (
    OPERATOR_TOKEN_ENV,
    Gateway,
    GatewayConfigError,
    Upstream,
)
from services.gateway.policy.model import Policy, PolicyError, load_policy


async def serve(args: argparse.Namespace) -> int:
    settings = load_settings()
    policy: Policy | None = None if args.policy == "none" else load_policy(args.policy)
    provider = setup_tracing("tradeguard-gateway", settings)
    replay_args = ["-m", "services.replay_server", "--scenario", args.scenario]
    if args.dev_tools:
        replay_args.append("--dev-tools")
    upstream = Upstream(
        name="replay",
        target=StdioServerParameters(
            command=sys.executable, args=replay_args, cwd=Path.cwd(), env=child_env()
        ),
    )
    try:
        async with AsyncExitStack() as stack:
            audit: AuditSink
            approvals: ApprovalStore
            if args.audit == "memory":
                memory_audit = MemoryAuditSink()
                audit = memory_audit
                approvals = MemoryApprovalStore(audit=memory_audit)
            else:
                url = settings.database_url.get_secret_value()
                await check_schema(url)
                audit = await stack.enter_async_context(PostgresAuditSink(url))
                approvals = await stack.enter_async_context(PostgresApprovalStore(url))
            gateway = await stack.enter_async_context(
                Gateway(
                    [upstream],
                    audit=audit,
                    session_id=args.session,
                    policy=policy,
                    operator_token=os.environ.get(OPERATOR_TOKEN_ENV) or None,
                    approvals=approvals,
                    approval_timeout_s=settings.approval_timeout_seconds,
                )
            )
            policy_name = policy.name if policy else "none (pass through)"
            print(f"gateway: session={args.session} upstreams=replay tools={len(gateway.tools)} "
                  f"audit={args.audit} policy={policy_name}", file=sys.stderr)  # fmt: skip
            try:
                async with stdio_server() as (read_stream, write_stream):
                    await gateway.server.run(
                        read_stream, write_stream, gateway.server.create_initialization_options()
                    )
            finally:
                stats = gateway.overhead_stats()
                print(f"gateway: overhead {json.dumps(stats)}", file=sys.stderr)
    finally:
        if provider is not None:
            provider.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway", description="TradeGuard gateway (stdio)")
    parser.add_argument("--session", required=True, help="agent session id, used in the audit log")
    parser.add_argument("--scenario", default="demo")
    parser.add_argument("--audit", choices=["postgres", "memory"], default="postgres")
    parser.add_argument("--dev-tools", action="store_true", help="expose advance_clock upstream")
    parser.add_argument(
        "--policy",
        default="policies/default.yaml",
        help="policy YAML, or 'none' for the F4 pass-through proxy",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(serve(args))
    except (ConfigError, GatewayConfigError, PolicyError, RuntimeError, OSError) as exc:
        # Fail closed: no gateway, no tools.
        print(f"gateway: refusing to start: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
