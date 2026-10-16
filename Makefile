.PHONY: help install up down logs ps check-config test lint format typecheck check fixtures replay replay-http inspector

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

install:
	uv sync

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
