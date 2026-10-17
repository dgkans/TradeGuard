"""Gateway overhead benchmark: the same calls direct to the replay server and through the
gateway (stdio, Postgres audit), measured from the client.

    uv run python -m services.gateway.bench --calls 300

Writes a JSON report to evals/reports/ and prints p50 and p95. Run on an idle machine.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from services.common.config import child_env, load_settings
from services.gateway.mcp_proxy.proxy import percentile

# A mix of cheap and heavier read only calls. No orders, so repeated runs stay comparable.
CALLS: list[tuple[str, dict[str, Any]]] = [
    ("get_quote", {"symbol": "NVDA"}),
    ("get_bars", {"symbol": "AMD", "interval": "5m", "limit": 50}),
    ("get_account", {}),
    ("scan_watchlist", {"symbols": ["NVDA", "AMD", "AAPL"]}),
]
WARMUP = 20


async def timed_calls(params: StdioServerParameters, count: int) -> list[float]:
    """Client observed latency in ms for `count` calls, after a warmup."""
    samples = []
    async with Client(params) as client:
        for i in range(WARMUP):
            name, args = CALLS[i % len(CALLS)]
            await client.call_tool(name, args)
        for i in range(count):
            name, args = CALLS[i % len(CALLS)]
            started = time.perf_counter()
            result = await client.call_tool(name, args)
            samples.append((time.perf_counter() - started) * 1000)
            if result.is_error:
                raise RuntimeError(f"{name} failed during benchmark: {result.content}")
    return samples


def server_side(session_id: str, url: str) -> dict[str, float]:
    """Overhead recorded by the gateway itself in audit_events (excludes warmup calls)."""
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "select overhead_ms, upstream_ms from audit_events where session_id = %s "
            "and latency_ms is not null order by id offset %s",
            (session_id, WARMUP),
        ).fetchall()
    overheads = [float(row[0]) for row in rows]
    return {
        "calls": float(len(rows)),
        "overhead_p50_ms": percentile(overheads, 50),
        "overhead_p95_ms": percentile(overheads, 95),
    }


def git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return result.stdout.strip()


def summary(samples: list[float]) -> dict[str, float]:
    return {"p50_ms": percentile(samples, 50), "p95_ms": percentile(samples, 95),
            "mean_ms": round(sum(samples) / len(samples), 3)}  # fmt: skip


async def main_async(count: int) -> dict[str, Any]:
    settings = load_settings()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    session_id = f"bench-{stamp}"
    cwd = Path.cwd()
    direct = StdioServerParameters(
        command=sys.executable, args=["-m", "services.replay_server", "--scenario", "demo"],
        cwd=cwd, env=child_env() | {"OTEL_SDK_DISABLED": "true"},
    )  # fmt: skip
    gateway = StdioServerParameters(
        command=sys.executable,
        args=["-m", "services.gateway", "--session", session_id, "--scenario", "demo"],
        cwd=cwd, env=child_env() | {"OTEL_SDK_DISABLED": "true"},
    )  # fmt: skip
    direct_ms = await timed_calls(direct, count)
    gateway_ms = await timed_calls(gateway, count)

    added = []
    for g, d in zip(sorted(gateway_ms), sorted(direct_ms), strict=True):
        added.append(g - d)
    commit = git(["rev-parse", "--short", "HEAD"])
    if git(["status", "--porcelain", "--untracked-files=no"]):
        commit += "-dirty"
    return {
        "date": stamp,
        "commit": commit,
        "calls_per_path": count,
        "warmup_calls": WARMUP,
        "call_mix": [name for name, _ in CALLS],
        "tracing": "disabled for both paths",
        "audit": "postgres (local Homebrew PostgreSQL 16)",
        "direct": summary(direct_ms),
        "through_gateway": summary(gateway_ms),
        "client_added_latency": {
            "p50_ms": round(percentile(gateway_ms, 50) - percentile(direct_ms, 50), 3),
            "p95_ms": round(percentile(gateway_ms, 95) - percentile(direct_ms, 95), 3),
        },
        "gateway_internal_overhead": server_side(
            session_id, settings.database_url.get_secret_value()
        ),
        "gateway_session_id": session_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=300)
    args = parser.parse_args(argv)
    report = asyncio.run(main_async(args.calls))
    out = Path("evals/reports") / f"gateway_overhead_{report['date']}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
