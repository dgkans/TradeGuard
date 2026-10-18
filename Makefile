.PHONY: help install up down logs ps check-config test lint format typecheck check fixtures replay replay-http inspector migrate jaeger bench-gateway scenarios eval-smoke eval-smoke-replay eval-full api dashboard-install dashboard dashboard-check

help:
	@echo "install       uv sync (Python deps + dev tools)"
	@echo "up            start Postgres, Jaeger, OTel Collector and wait until healthy"
	@echo "down          stop the local stack"
	@echo "check-config  validate .env (fails if live trading is configured)"
	@echo "test          run pytest"
	@echo "lint          ruff lint + format check"
	@echo "typecheck     mypy"
	@echo "check         lint + typecheck + test (same as CI)"
	@echo "fixtures      re-download bar fixtures (overwrites committed data)"
	@echo "replay        replay MCP server on stdio (SCENARIO=demo)"
	@echo "replay-http   replay MCP server on http://127.0.0.1:8001/mcp with dev tools"
	@echo "inspector     open the MCP Inspector in the browser against the replay server"
	@echo "migrate       apply database migrations (alembic upgrade head)"
	@echo "jaeger        run local Jaeger (UI http://localhost:16686, OTLP :4317/:4318)"
	@echo "bench-gateway measure gateway overhead: direct vs through the gateway"
	@echo "scenarios     regenerate the eval scenario suite and its news fixtures"
	@echo "eval-smoke    10 scenario smoke eval, full_guard, live model; records tapes"
	@echo "eval-smoke-replay  smoke eval from the committed tapes (no model needed)"
	@echo "eval-full     all scenarios x 3 configs x 3 repeats, live model (hours)"
	@echo "api           dashboard API on http://127.0.0.1:8000 (approvals, audit, chat, evals)"
	@echo "dashboard-install  npm ci for the dashboard"
	@echo "dashboard     dashboard dev server on http://127.0.0.1:5173 (needs `make api`)"
	@echo "dashboard-check    dashboard lint + typecheck + tests + build"

install:
	uv sync
	@# macOS: CPython skips .pth files flagged hidden (iCloud-synced folders can set it), which
	@# breaks the editable install behind `uv run tradeguard`. See DECISIONS.md D007.
	@if [ "$$(uname)" = "Darwin" ]; then chflags -R nohidden .venv; fi

up: check-config
	docker compose up -d --wait
	@curl -fsS http://localhost:13133/ > /dev/null && echo "otel collector healthy"
	@echo "Jaeger UI: http://localhost:16686"

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

check-config:
	uv run python -m services.common.config

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

check: lint typecheck test

SCENARIO ?= demo

fixtures:
	uv run python -m services.replay_server.download

replay:
	uv run python -m services.replay_server --scenario $(SCENARIO)

replay-http:
	uv run python -m services.replay_server --scenario $(SCENARIO) --transport http --dev-tools

inspector:
	npx -y @modelcontextprotocol/inspector@2.11.0 uv run python -m services.replay_server --scenario $(SCENARIO) --dev-tools

migrate:
	uv run alembic upgrade head

# Local Jaeger without Docker: official release binary, sha256 verified (DECISIONS.md D008).
jaeger:
	./.tools/jaeger

bench-gateway:
	uv run python -m services.gateway.bench --calls 300

scenarios:
	uv run python -m evals.build_scenarios

eval-smoke:
	uv run python -m evals.runner --smoke --mode record --tapes evals/tapes/smoke --out evals/reports/smoke_latest

eval-smoke-replay:
	uv run python -m evals.runner --smoke --mode replay --tapes evals/tapes/smoke --out evals/reports/smoke_replay

eval-full:
	uv run python -m evals.runner --repeats 3 --mode record --concurrency 2

api:
	uv run tradeguard api

dashboard-install:
	cd dashboard && npm ci

dashboard:
	cd dashboard && npx vite --host 127.0.0.1

dashboard-check:
	cd dashboard && npm run check
