.PHONY: help install up down logs ps check-config test lint format typecheck check

help:
	@echo "install       uv sync (Python deps + dev tools)"
	@echo "up            start Postgres, Jaeger, OTel Collector and wait until healthy"
	@echo "down          stop the local stack"
	@echo "check-config  validate .env (fails if live trading is configured)"
	@echo "test          run pytest"
	@echo "lint          ruff lint + format check"
	@echo "typecheck     mypy"
	@echo "check         lint + typecheck + test (same as CI)"

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
