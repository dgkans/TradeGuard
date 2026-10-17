# Progress

| Feature | Status | Date done |
|---|---|---|
| F0 Skeleton | done (compose + CI checks deferred to first push) | 2026-10-16 |
| F1 Replay server | done | 2026-10-16 |
| F2 Pattern detectors | done | 2026-10-16 |
| F3 Baseline agent | not started | |
| F4 Gateway proxy | not started | |
| F5 Policy engine | not started | |
| F6 Approval flow | not started | |
| F7 Taint tracking | not started | |
| F8 Intent alignment | not started | |
| F9 Eval harness | not started | |
| F10 Dashboard | not started | |
| F11 Alpaca paper | not started | |
| F12 Deployment | not started | |
| F13 Impact and beta | not started | |
| F14 Ship it | not started | |

## F0 Skeleton
### Plan
- Create the Section 5 layout (empty packages and `.gitkeep` placeholders).
- `pyproject.toml` managed by uv, Python 3.12 pinned, lockfile committed. Dev tools: ruff, mypy (strict on `services/`), pytest, pytest-asyncio.
- `services/common/config.py`: pydantic + python-dotenv loader (D001) that validates every env var at startup and names the bad field.
- Safety switch inside the loader: refuse to start if `LIVE_TRADING` is true, `TRADING_MODE` is not `replay`/`paper`, `ALPACA_PAPER` is false, `ALPACA_BASE_URL` is not the Alpaca paper host, or `LLM_PROVIDER=anthropic` (Section 16).
- `python -m services.common.config` checks the env and exits non zero with a clear message.
- `.env.example` documenting every variable.
- `docker-compose.yml`: Postgres, Jaeger, OTel Collector, each with a healthcheck; `make up` waits for healthy.
- `Makefile`: `up`, `down`, `test`, `lint`, `typecheck`, `check`.
- `.github/workflows/ci.yml`: ruff, mypy, pytest on every push.
### Acceptance
- [ ] `docker compose up` starts Postgres, Jaeger and Collector with healthy checks
- [x] `uv run pytest` passes with a placeholder test
- [ ] CI passes on push
- [x] Startup fails with a clear error when live mode is configured (tested)
### Notes and open questions
- Docker is not installed on the dev Mac, so the compose stack has not been started yet. Needs Docker Desktop or OrbStack (both free for personal use).
- CI needs a push to GitHub (`dgkans/TradeGuard`) to verify. User decision 2026-10-16: push only at the end of the build, so this and the compose check (CI runs it too) are deferred, not blocking.
### Loop summary
- Built: repo layout, uv project (Python 3.12, lockfile), ruff/mypy/pytest config, env loader with live trading safety switch, `.env.example`, compose stack (Postgres, Jaeger v2, OTel Collector), Makefile, CI workflow (Python job + compose health job).
- Tests: 19 passing locally; ruff and mypy strict clean.
- Evals: none (not applicable to F0).
- Gaps: compose stack not started (no Docker on dev Mac); CI not yet run (not pushed). Deferred to the first push (user decision).
- Next: F1 Replay MCP server.

## F1 Replay MCP server
### Plan
- `download.py`: fetch 1d (2y) and 5m (60d, Yahoo's limit) bars for 20 symbols into `fixtures/bars/<interval>/<SYM>.parquet`; commit the files.
- `bars.py`: bar store; every timestamp is the bar start in UTC; a bar is visible only after it closes.
- `scenario.py`: YAML scenario model and loader (`--scenario demo` finds `scenarios/**/demo.yaml`).
- `news.py`: synthetic news fixtures, `trusted: false`; the eval-only `injection` field is stripped before anything reaches the agent.
- `broker.py`: cash account sim broker. Market/limit orders by qty or notional, fill at next 5m bar open (limit fills intra bar only once that bar has closed), day orders expire at the close, buying power and position checks, seeded order ids.
- `session.py`: one replay session = clock + broker + stores; implements the 8 tools plus read only `get_clock`; `advance()` moves time and processes fills.
- `server.py` / `__main__.py`: MCP SDK v2 `MCPServer`, stdio and streamable HTTP; `advance_clock` tool only with `--dev-tools`.
- Tests: determinism, no future data (bars, quotes, news, fills), buying power rejection, both transports.
### Acceptance
- [x] Same scenario seed returns identical data and fills on repeated runs (tested)
- [x] No tool can return data from the future relative to the sim clock (tested)
- [x] Sim broker rejects orders exceeding buying power (tested)
- [x] Server runs over stdio and streamable HTTP (tested; both transports spawned as subprocesses)
### Notes and open questions
- Yahoo serves 5m bars only for the last 60 days, so intraday scenarios must start between 2026-07-24 and 2026-10-16. Fixtures are committed, so this window is fixed from now on.
- Found during the demo run and fixed with regression tests: float32 price noise from Yahoo; a market order placed exactly on a bar boundary filled at that same bar's open instead of the next one.
- F5 must add `get_clock` to the read allowlist (D005).
### Loop summary
- Built: replay MCP server (MCP SDK 2.3 `MCPServer`) with 9 agent tools + dev-only `advance_clock`, bar store, sim clock, cash account sim broker, scenario loader, demo scenario, 4 synthetic news items (1 injection), committed bar fixtures for 20 symbols.
- Tests: 55 passing (36 new for F1); ruff and mypy strict clean.
- Evals: none (smoke eval starts once the agent exists).
- Gaps: no holiday calendar (D004); intraday scenarios limited to 2026-07-24..2026-10-16; F0 compose/CI checks still deferred to the first push.
- Next: F2 Price action pattern detectors.

## Current feature: F2 Price action pattern detectors
### Plan
- `services/patterns/` holds pure functions over any bar with `start, open, high, low, close, volume` (a Protocol, so it has no dependency on the replay server).
- `atr(bars, n=14)`: Wilder ATR. `None` when there are too few bars.
- `inside_bar(bars)`: last daily bar strictly inside the one before it.
- `prior_day_high_break(prior_day, today_bars)`: first closed 5m bar today that *closes* above yesterday's high (a wick alone doesn't count).
- `bull_flag(bars)`: simplified rules: pole of 3 to 7 bars gaining at least 8%, then a flag of 2 to 6 bars with no new high, a retrace of at most 50% of the pole, highs drifting flat or down, and lower average volume than the pole.
- `support_resistance(bars)`: swing pivots (2 bars each side) clustered within 0.5 ATR, split around the last close, nearest first, with touch counts.
- Every detection returns the rule id, a plain English rule, the numbers that satisfied it, and the supporting bars by role and timestamp.
- Replay tools `detect_patterns(symbol)` and `scan_watchlist(symbols)` use only bars closed at the sim clock's "now".
- Tests: hand built bar sequences, positive and negative cases, boundaries; tool tests check that no output timestamp is after now.
### Acceptance
- [x] Each detector has unit tests on hand built bar sequences, including negative cases
- [x] Output explains which rule fired with bar timestamps
- [x] Rules documented in `services/patterns/README.md`
### Notes and open questions
- Demo run showed NVDA's prior day high break had already failed by 10:30, with nothing saying so. Added `holding_above_high` and a `latest_bar` reference (regression test).
- Sanity sweep (dev check, not an eval metric): 1,180 symbol-days, inside bar 126, prior day high break 449, bull flag 33 (D006).
### Loop summary
- Built: `services/patterns/` (ATR, inside bar, prior day high break, simplified bull flag, support/resistance, analyze/summarize) and replay tools `detect_patterns` and `scan_watchlist`.
- Tests: 84 passing (29 new for F2); ruff and mypy strict clean.
- Evals: none (the agent doesn't exist yet).
- Gaps: thresholds are fixed, not tuned; levels use daily bars only; the bull flag is daily only, no intraday flags.
- Next: F3 Baseline LangGraph agent (no guard).
