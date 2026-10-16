# Progress

| Feature | Status | Date done |
|---|---|---|
| F0 Skeleton | in progress | |
| F1 Replay server | not started | |
| F2 Pattern detectors | not started | |
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

## Current feature: F0
### Plan
- Create the Section 5 layout (empty packages and `.gitkeep` placeholders).
- `pyproject.toml` managed by uv, Python 3.12 pinned, lockfile committed. Dev tools: ruff, mypy (strict on `services/`), pytest, pytest-asyncio.
- `services/common/config.py`: pydantic-settings loader that validates every env var at startup and names the bad field.
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
- CI needs a push to GitHub (`dgkans/TradeGuard`) to verify.
### Loop summary
- Built: repo layout, uv project (Python 3.12, lockfile), ruff/mypy/pytest config, env loader with live trading safety switch, `.env.example`, compose stack (Postgres, Jaeger v2, OTel Collector), Makefile, CI workflow (Python job + compose health job).
- Tests: 19 passing locally; ruff and mypy strict clean.
- Evals: none (not applicable to F0).
- Gaps: compose stack not started (no Docker on dev Mac); CI not yet run (not pushed). F0 stays "in progress" until both are verified.
- Next: F1 Replay MCP server, once F0's two open criteria pass.
