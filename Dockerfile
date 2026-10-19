# TradeGuard API: the dashboard API, trader sessions, gateway and live prices in one
# process (F12). Secrets come from the host's environment at run time, never the image.
#
#   docker build -t tradeguard .
#   docker run -p 8000:8000 --env-file prod.env tradeguard

FROM python:3.12.14-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY services ./services
RUN uv sync --locked --no-dev

FROM python:3.12.14-slim-trixie
RUN useradd --create-home --uid 10001 tradeguard
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/services ./services
COPY pyproject.toml alembic.ini ./
COPY migrations ./migrations
COPY policies ./policies
COPY scenarios ./scenarios
COPY scenarios_hard ./scenarios_hard
COPY fixtures ./fixtures
COPY evals/reports ./evals/reports
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TRADEGUARD_ENV=production \
    API_HOST=0.0.0.0 \
    OTEL_SDK_DISABLED=true
USER tradeguard
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz', timeout=4)"
# Migrate, then serve on the port the host gives us (Render sets PORT).
CMD ["sh", "-c", "alembic upgrade head && API_PORT=${PORT:-8000} exec tradeguard api"]
