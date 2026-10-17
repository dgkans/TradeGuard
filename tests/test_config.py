"""Tests for the startup config loader and the live trading safety switch."""

import subprocess
import sys
from pathlib import Path

import pytest

from services.common.config import (
    ConfigError,
    LiveTradingForbiddenError,
    Settings,
    load_settings,
)

ENV_VARS = [
    "TRADEGUARD_ENV",
    "TRADING_MODE",
    "LIVE_TRADING",
    "ALPACA_PAPER",
    "ALPACA_BASE_URL",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "DATABASE_URL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_SERVICE_NAME",
    "OTEL_SDK_DISABLED",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "OLLAMA_BASE_URL",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "LOG_LEVEL",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an empty environment so the developer's shell can't leak in."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def load(**env: str) -> Settings:
    return load_settings(env_file=None, environ=env)


def test_defaults_are_safe() -> None:
    settings = load()
    assert settings.trading_mode == "replay"
    assert settings.live_trading is False
    assert settings.alpaca_paper is True
    assert settings.llm_provider == "ollama"


def test_paper_mode_with_paper_endpoint_is_allowed() -> None:
    settings = load(TRADING_MODE="paper", ALPACA_BASE_URL="https://paper-api.alpaca.markets")
    assert settings.trading_mode == "paper"


@pytest.mark.parametrize(
    ("env", "expected_field"),
    [
        ({"LIVE_TRADING": "true"}, "LIVE_TRADING"),
        ({"LIVE_TRADING": "1"}, "LIVE_TRADING"),
        ({"TRADING_MODE": "live"}, "TRADING_MODE"),
        ({"TRADING_MODE": "LIVE"}, "TRADING_MODE"),
        ({"ALPACA_PAPER": "false"}, "ALPACA_PAPER"),
        ({"ALPACA_BASE_URL": "https://api.alpaca.markets"}, "ALPACA_BASE_URL"),
        ({"ALPACA_BASE_URL": "https://api.alpaca.markets/v2"}, "ALPACA_BASE_URL"),
        # A lookalike host must not pass a substring check.
        ({"ALPACA_BASE_URL": "https://paper-api.alpaca.markets.evil.com"}, "ALPACA_BASE_URL"),
        ({"ALPACA_BASE_URL": "http://paper-api.alpaca.markets"}, "ALPACA_BASE_URL"),
    ],
)
def test_live_configuration_is_refused(env: dict[str, str], expected_field: str) -> None:
    with pytest.raises(LiveTradingForbiddenError) as excinfo:
        load(**env)
    message = str(excinfo.value)
    assert expected_field in message
    assert "live trading" in message.lower()


def test_anthropic_provider_is_disabled() -> None:
    # Section 16: the app must not make paid Anthropic API calls.
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        load(LLM_PROVIDER="anthropic")


def test_invalid_value_names_the_field() -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load(LOG_LEVEL="loud")


def test_unknown_trading_mode_names_the_field() -> None:
    with pytest.raises(ConfigError, match="TRADING_MODE"):
        load(TRADING_MODE="yolo")


def test_secrets_are_not_in_repr() -> None:
    settings = load(ALPACA_API_KEY="PKSECRET123", GROQ_API_KEY="gsk_secret")
    text = repr(settings)
    assert "PKSECRET123" not in text
    assert "gsk_secret" not in text


REPO_ROOT = Path(__file__).resolve().parent.parent


def run_check(cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the startup check in a temp dir so a developer's own .env can't affect it."""
    full_env = {"PATH": "", "PYTHONPATH": str(REPO_ROOT), **env}
    return subprocess.run(
        [sys.executable, "-m", "services.common.config"],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_check_command_exits_nonzero_in_live_mode(tmp_path: Path) -> None:
    """The startup check used by services must fail closed with a readable message."""
    result = run_check(tmp_path, {"LIVE_TRADING": "true"})
    assert result.returncode != 0
    assert "LIVE_TRADING" in result.stderr
    assert "Traceback" not in result.stderr


def test_live_setting_in_env_file_is_refused(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TRADING_MODE=live\n")
    result = run_check(tmp_path, {})
    assert result.returncode != 0
    assert "TRADING_MODE" in result.stderr


def test_check_command_succeeds_with_defaults(tmp_path: Path) -> None:
    result = run_check(tmp_path, {})
    assert result.returncode == 0, result.stderr
    assert "replay" in result.stdout


def test_otel_sdk_disabled_turns_tracing_off() -> None:
    """Regression: an empty OTEL endpoint falls back to the default, so disabling needs
    the explicit standard switch."""
    from services.common.telemetry import setup_tracing

    assert setup_tracing("t", load(OTEL_SDK_DISABLED="true")) is None
    assert load(OTEL_EXPORTER_OTLP_ENDPOINT="").otel_exporter_otlp_endpoint != ""


def test_child_env_forwards_tradeguard_settings_only() -> None:
    """Regression: MCP stdio children only inherit a small env allowlist, so settings set
    in the shell (DATABASE_URL, OTEL_*) were silently lost in the gateway and replay server."""
    from services.common.config import child_env

    env = child_env(
        {
            "DATABASE_URL": "postgresql://x@db/y",
            "OTEL_SDK_DISABLED": "true",
            "OTEL_RESOURCE_ATTRIBUTES": "a=b",
            "LLM_MODEL": "qwen3:8b",
            "AWS_SECRET_ACCESS_KEY": "nope",
            "HOME": "/Users/x",
        }
    )
    assert env == {
        "DATABASE_URL": "postgresql://x@db/y",
        "OTEL_SDK_DISABLED": "true",
        "OTEL_RESOURCE_ATTRIBUTES": "a=b",
        "LLM_MODEL": "qwen3:8b",
    }


def test_child_env_forwards_operator_token() -> None:
    from services.common.config import OPERATOR_TOKEN_ENV, child_env

    assert child_env({OPERATOR_TOKEN_ENV: "t"}) == {OPERATOR_TOKEN_ENV: "t"}
