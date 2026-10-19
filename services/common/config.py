"""Startup configuration for every TradeGuard process.

All settings come from environment variables, optionally seeded from a `.env` file.
Values already in the process environment win over the file.

The safety switch runs before normal validation and fails closed: any setting that could
point the system at a live brokerage account stops startup. TradeGuard is paper and
replay only (PLAN.md Section 3, Loop rule 3).
"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

ALPACA_PAPER_HOST = "paper-api.alpaca.markets"
# Name of the env var carrying the gateway operator token (services/gateway).
OPERATOR_TOKEN_ENV = "TRADEGUARD_OPERATOR_TOKEN"  # noqa: S105 - a variable name

# Anything outside these sets is treated as "on" / "not paper". Unknown spellings are
# refused rather than guessed, so a typo can never enable live trading.
FALSE_WORDS = {"", "0", "false", "no", "off"}
TRUE_WORDS = {"1", "true", "yes", "on"}


class ConfigError(Exception):
    """The environment is invalid. The message names every bad variable."""


class LiveTradingForbiddenError(ConfigError):
    """The environment asks for live trading, which TradeGuard never allows."""


class Settings(BaseModel):
    """Validated process settings. Field aliases are the environment variable names."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    tradeguard_env: Literal["development", "test", "production"] = Field(
        default="development", alias="TRADEGUARD_ENV"
    )
    trading_mode: Literal["replay", "paper"] = Field(default="replay", alias="TRADING_MODE")
    live_trading: bool = Field(default=False, alias="LIVE_TRADING")
    alpaca_paper: bool = Field(default=True, alias="ALPACA_PAPER")
    alpaca_base_url: str = Field(default=f"https://{ALPACA_PAPER_HOST}", alias="ALPACA_BASE_URL")
    alpaca_api_key: SecretStr | None = Field(default=None, alias="ALPACA_API_KEY")
    alpaca_secret_key: SecretStr | None = Field(default=None, alias="ALPACA_SECRET_KEY")

    database_url: SecretStr = Field(
        default=SecretStr("postgresql://tradeguard:tradeguard@localhost:5432/tradeguard"),
        alias="DATABASE_URL",
    )

    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4318", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    otel_service_name: str = Field(default="tradeguard", alias="OTEL_SERVICE_NAME")
    otel_sdk_disabled: bool = Field(default=False, alias="OTEL_SDK_DISABLED")

    llm_provider: Literal["ollama", "groq", "gemini"] = Field(
        default="ollama", alias="LLM_PROVIDER"
    )
    llm_model: str = Field(default="qwen3:8b", alias="LLM_MODEL")
    judge_model: str = Field(default="qwen3:8b", alias="JUDGE_MODEL")
    ollama_base_url: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")
    # A smaller model for the quick JSON steps (classify the request, the intent judge's
    # default); empty means LLM_MODEL. On Groq each model has its own free budget (F12).
    llm_fast_model: str = Field(default="", alias="LLM_FAST_MODEL")
    groq_api_key: SecretStr | None = Field(default=None, alias="GROQ_API_KEY")
    gemini_api_key: SecretStr | None = Field(default=None, alias="GEMINI_API_KEY")

    approval_timeout_seconds: float = Field(default=300, gt=0, alias="APPROVAL_TIMEOUT_SECONDS")
    api_host: str = Field(default="127.0.0.1", alias="API_HOST")
    api_port: int = Field(default=8000, gt=0, lt=65536, alias="API_PORT")
    jaeger_ui_url: str = Field(default="http://localhost:16686", alias="JAEGER_UI_URL")
    admin_password: SecretStr | None = Field(default=None, alias="ADMIN_PASSWORD")
    trader_token_secret: SecretStr | None = Field(default=None, alias="TRADER_TOKEN_SECRET")
    # Live prices for the trading desk (F10f): Yahoo Finance, polled politely.
    live_prices: bool = Field(default=True, alias="LIVE_PRICES")
    live_poll_seconds: float = Field(default=30, ge=10, le=3600, alias="LIVE_POLL_SECONDS")
    live_closed_poll_seconds: float = Field(
        default=300, ge=30, le=3600, alias="LIVE_CLOSED_POLL_SECONDS"
    )
    paper_watchlist: str = Field(default="NVDA,AMD,AAPL,MSFT,TSLA", alias="PAPER_WATCHLIST")
    # Public deployment (F12). Caps are AI turns per UTC day; 0 means no cap.
    llm_daily_turn_cap: int = Field(default=0, ge=0, alias="LLM_DAILY_TURN_CAP")
    trader_daily_turn_cap: int = Field(default=0, ge=0, alias="TRADER_DAILY_TURN_CAP")
    trader_history_turns: int = Field(default=0, ge=0, le=50, alias="TRADER_HISTORY_TURNS")
    signups_per_ip_per_hour: int = Field(default=5, ge=1, alias="SIGNUPS_PER_IP_PER_HOUR")
    cors_origins: str = Field(default="", alias="CORS_ORIGINS")  # comma separated
    trust_proxy: bool = Field(default=False, alias="TRUST_PROXY")  # behind Render's proxy

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )

    @field_validator("llm_provider", mode="before")
    @classmethod
    def reject_paid_provider(cls, value: object) -> object:
        """Block the paid Anthropic API with a specific message (PLAN.md Section 16)."""
        if isinstance(value, str) and value.strip().lower() == "anthropic":
            raise ValueError(
                "anthropic is disabled: the app must run on free providers "
                "(ollama, groq, gemini). See PLAN.md Section 16."
            )
        return value

    @model_validator(mode="after")
    def production_needs_its_secrets(self) -> "Settings":
        """A public deployment must not start half configured (fail closed, F12)."""
        if self.tradeguard_env != "production":
            return self
        missing = []
        if self.admin_password is None:
            missing.append("ADMIN_PASSWORD")
        if self.trader_token_secret is None:
            missing.append("TRADER_TOKEN_SECRET")
        if self.llm_provider == "ollama":
            missing.append("LLM_PROVIDER (a cloud provider, e.g. groq)")
        if self.llm_provider == "groq" and self.groq_api_key is None:
            missing.append("GROQ_API_KEY")
        if missing:
            raise ValueError("production needs " + ", ".join(missing))
        return self

    @field_validator("log_level", mode="before")
    @classmethod
    def upper_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment to pass to TradeGuard child processes (gateway, replay server).

    MCP stdio clients start children with only a minimal allowlist (PATH, HOME, ...), so
    settings exported in the shell would be lost. Forward every variable Settings knows
    about plus OTEL_*, and nothing else, so unrelated secrets stay out of children.
    """
    source = os.environ if environ is None else environ
    known = set()
    for field in Settings.model_fields.values():
        if field.alias:
            known.add(field.alias)
    forwarded = {}
    for key, value in source.items():
        if key in known or key.startswith("OTEL_") or key == OPERATOR_TOKEN_ENV:
            forwarded[key] = value
    return forwarded


def check_live_trading(raw: Mapping[str, str]) -> None:
    """Refuse any raw environment that could reach a live brokerage endpoint.

    Inputs: the raw, unvalidated environment mapping.
    Output: None when the environment is safe.
    Failure: raises LiveTradingForbiddenError listing every offending variable.
    """
    problems: list[str] = []

    live_flag = raw.get("LIVE_TRADING", "").strip().lower()
    if live_flag not in FALSE_WORDS:
        problems.append(f"LIVE_TRADING={raw['LIVE_TRADING']!r} must be false")

    mode = raw.get("TRADING_MODE", "").strip().lower()
    if mode == "live":
        problems.append("TRADING_MODE='live' is not allowed; use 'replay' or 'paper'")

    if "ALPACA_PAPER" in raw:
        paper_flag = raw["ALPACA_PAPER"].strip().lower()
        if paper_flag not in TRUE_WORDS:
            problems.append(f"ALPACA_PAPER={raw['ALPACA_PAPER']!r} must be true")

    base_url = raw.get("ALPACA_BASE_URL", "").strip()
    if base_url and not is_alpaca_paper_url(base_url):
        problems.append(f"ALPACA_BASE_URL={base_url!r} must be https://{ALPACA_PAPER_HOST}")

    if problems:
        details = "; ".join(problems)
        raise LiveTradingForbiddenError(f"Refusing to start: live trading is forbidden. {details}")


def is_alpaca_paper_url(url: str) -> bool:
    """True only for https URLs whose host is exactly the Alpaca paper host."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    if parts.hostname != ALPACA_PAPER_HOST:
        return False
    return parts.port in (None, 443)


def read_environment(
    env_file: str | Path | None, environ: Mapping[str, str] | None
) -> dict[str, str]:
    """Merge the optional .env file under the process environment."""
    merged: dict[str, str] = {}
    if env_file is not None and Path(env_file).is_file():
        for key, value in dotenv_values(env_file).items():
            if value is not None:
                merged[key] = value
    source = os.environ if environ is None else environ
    for key, value in source.items():
        merged[key] = value
    return merged


def load_settings(
    env_file: str | Path | None = ".env", environ: Mapping[str, str] | None = None
) -> Settings:
    """Read, safety check and validate settings.

    Inputs: an optional .env path and an optional environment mapping
    (defaults to os.environ).
    Output: a frozen Settings object.
    Failure: LiveTradingForbiddenError for any live trading configuration,
    ConfigError for any other invalid value. Both name the variables at fault.
    """
    raw = read_environment(env_file, environ)
    check_live_trading(raw)

    # Empty strings in .env mean "unset" so defaults apply.
    present: dict[str, str] = {}
    for key, value in raw.items():
        if value.strip() != "":
            present[key] = value.strip()

    try:
        return Settings.model_validate(present)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc)) from None


def format_validation_error(exc: ValidationError) -> str:
    lines = ["Invalid configuration:"]
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"])
        lines.append(f"  {field}: {error['msg']}")
    return "\n".join(lines)


def main() -> int:
    """Validate the environment and print a short summary. Exit code 0 means safe."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        f"config ok: env={settings.tradeguard_env} mode={settings.trading_mode} "
        f"llm={settings.llm_provider}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
