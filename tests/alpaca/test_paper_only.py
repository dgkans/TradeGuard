"""The gateway refuses to start an Alpaca upstream unless paper mode is explicitly configured."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Set explicitly so a real paper setup in ./.env can never make these cases connect
# (process environment variables override .env).
SAFE_BASE = {
    "TRADING_MODE": "replay",
    "ALPACA_API_KEY": "",
    "ALPACA_SECRET_KEY": "",
    "ALPACA_PAPER": "true",
    "ALPACA_BASE_URL": "https://paper-api.alpaca.markets",
    "LIVE_TRADING": "false",
    "DATABASE_URL": "postgresql://x@127.0.0.1:1/x",
}


def start_gateway(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")}
    return subprocess.run(
        [sys.executable, "-m", "services.gateway", "--session", "t", "--upstream", "alpaca",
         "--audit", "memory", "--policy", "policies/default.yaml"],
        cwd=REPO_ROOT, env={**base, "OTEL_SDK_DISABLED": "true", **SAFE_BASE, **env},
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({}, "TRADING_MODE=paper"),
        ({"TRADING_MODE": "paper"}, "ALPACA_API_KEY"),
        ({"TRADING_MODE": "paper", "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s",
          "ALPACA_PAPER": "false"}, "ALPACA_PAPER"),
        ({"TRADING_MODE": "paper", "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s",
          "ALPACA_BASE_URL": "https://api.alpaca.markets"}, "ALPACA_BASE_URL"),
        ({"TRADING_MODE": "live", "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"},
         "TRADING_MODE"),
        ({"LIVE_TRADING": "true", "TRADING_MODE": "paper"}, "LIVE_TRADING"),
    ],
)  # fmt: skip
def test_gateway_refuses_to_start_without_explicit_paper_mode(
    env: dict[str, str], reason: str
) -> None:
    result = start_gateway(env)
    assert result.returncode == 2, result.stderr
    assert reason in result.stderr
    assert "refusing to start" in result.stderr or "live trading" in result.stderr.lower()
