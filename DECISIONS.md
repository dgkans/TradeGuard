# Decisions

## D001: Config loader is plain pydantic + python-dotenv, with the safety switch before validation
Date: 2026-10-16
Context: F0 needs env validation at startup and a switch that refuses live trading.
Decision: `services/common/config.py` merges `.env` under the process environment, runs
`check_live_trading` on the raw strings, then validates into a frozen pydantic `Settings`
model whose aliases are the env var names. Unknown spellings of `LIVE_TRADING` /
`ALPACA_PAPER` are refused rather than guessed. `ALPACA_BASE_URL` must be https with host
exactly `paper-api.alpaca.markets` (no substring match). `LLM_PROVIDER=anthropic` is refused
(Section 16). Secrets, including `DATABASE_URL`, are `SecretStr` so they never print.
Alternatives considered: pydantic-settings. Its env reading is tied to `os.environ`, which
makes tests that pass an explicit mapping awkward, and its error for `TRADING_MODE=live`
would be a generic enum error instead of a live trading refusal.
Consequences: every service should call `load_settings()` first thing.
`python -m services.common.config` is the standalone check, also run by `make up`.

## D002: mypy strict on all of `services/`, not only the gateway
Date: 2026-10-16
Context: Section 4 asks for strict on the gateway. The gateway imports `services.common`.
Decision: strict for `services.*`; tests are type checked in normal mode.
Alternatives considered: strict only on `services.gateway.*`.
Consequences: slightly more typing work in agent and replay code. Can be relaxed per module
later if a third party library without stubs makes it impractical.

## D003: Local stack images and healthchecks
Date: 2026-10-16
Context: `docker compose up` must report all three services healthy.
Decision: `postgres:16.10-alpine` (`pg_isready`), `jaegertracing/jaeger:2.10.0` (Jaeger v2;
v1 is end of life) probed on its 13133 health endpoint, and
`otel/opentelemetry-collector-contrib:0.135.0`. The collector image is distroless with no
shell or HTTP client, so its in-container healthcheck runs `otelcol-contrib validate`, and
`make up` plus CI also probe its real health endpoint from the host.
Alternatives considered: a custom collector image with curl added (more to maintain);
skipping the collector healthcheck (fails the acceptance criterion).
Consequences: the collector's container status shows "config valid", not live health. The
host side probe covers that. **Not yet verified on a Docker host** (see PROGRESS.md).

## D004: Bar fixtures come from Yahoo via yfinance and are committed
Date: 2026-10-16
Context: F1 needs deterministic historical bars with no paid data plan (Section 16).
Decision: `services/replay_server/download.py` pulls 20 symbols (10 in the default policy
allowlist, 10 outside it): 2 years of daily bars and 60 days of 5 minute bars, regular hours
only, `auto_adjust=False`. Stored as Parquet with the bar START in UTC (daily bars stamped
at 09:30 New York). Committed (3.8 MB) so replays never touch the network. Prices are
rounded to 4 decimals on load because Yahoo returns float32 values.
Alternatives considered: Alpaca historical data (needs keys, so not usable in CI or by
visitors); synthetic random walks (not realistic enough for the price action detectors in F2).
Consequences: Yahoo only serves 60 days of 5 minute data, so the intraday window is fixed at
2026-07-24 to 2026-10-16. Every scenario that needs intraday data must start inside it.
Exchange holidays and half days are not modeled: a daily bar becomes visible at 16:00 New
York even on a half day. That reveals data later than reality, never earlier, so it can't
leak the future.

## D005: Sim broker and clock rules
Date: 2026-10-16
Context: F1 acceptance requires determinism and no future data, including through fills.
Decision:
- A bar is visible only once it has closed. A quote is the open of the 5 minute bar in
  progress (a real trade at that instant), else the last close.
- Orders fill only on bars starting strictly after submission. Market orders fill at that
  open. A limit order fills at the open if the open crosses the limit; otherwise it fills
  at the limit only after the bar that touched it has closed.
- Day orders expire at the next 16:00 New York close. Cash account: no margin, no shorting;
  open orders reserve cash or shares, so two orders can't spend the same buying power.
- Broker refusals return an order with `status: rejected` and a reason, as a broker would.
  Bad arguments (unknown symbol, both qty and notional, ...) are MCP tool errors instead.
- Order ids come from `random.Random(scenario.seed)`, so repeated runs are identical.
- Two tools beyond the plan's list: `get_clock` (read only, so the agent can tell whether
  the market is open) and `advance_clock`, registered only with `--dev-tools`, so the agent
  can never move time. The eval harness drives time through `ReplaySession.advance`.
Alternatives considered: filling at the bar close (looks ahead within the bar); letting
the broker allow shorts (would add margin modeling; F5 policy denies shorts anyway).
Consequences: the F5 policy `tool_allowlist.read` must add `get_clock`.
