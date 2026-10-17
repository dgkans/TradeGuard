# TradeGuard

Brokers check the margin. TradeGuard checks the agent.

TradeGuard is a safety and evaluation gateway for LLM trading agents. It sits between an
agent and its tools (MCP) and decides ALLOW, DENY or ESCALATE for every call. **Paper and
replay only**: the app refuses to start if live trading is configured.

Status: early build. See [PLAN.md](PLAN.md) for the design and [PROGRESS.md](PROGRESS.md)
for what is done.

## Run locally

Requirements: [uv](https://docs.astral.sh/uv/), make, PostgreSQL 16 (Docker Compose or
`brew install postgresql@16`), and [Ollama](https://ollama.com) with `ollama pull qwen3:8b`
for the agent.

```bash
cp .env.example .env     # every variable is documented in the file
make install             # Python 3.12 + deps via uv
make up                  # Postgres, Jaeger, OTel Collector; waits for healthchecks
make check               # ruff, mypy, pytest (same as CI)
```

- Jaeger UI: http://localhost:16686
- OTLP: `localhost:4317` (gRPC), `localhost:4318` (HTTP)
- Postgres: `localhost:5432`

`make check-config` validates your `.env`. It exits non zero with the offending variable
named if anything points at live trading (`LIVE_TRADING`, `TRADING_MODE=live`,
`ALPACA_PAPER=false`, or a non paper `ALPACA_BASE_URL`).

## Replay MCP server (F1)

A deterministic market simulator the agent talks to over MCP: historical bars for 20
symbols, synthetic news, and a simulated cash account broker. The clock is simulated and
no tool ever returns data from after "now".

```bash
make replay                 # stdio, demo scenario
make replay-http            # http://127.0.0.1:8001/mcp, plus the advance_clock dev tool
make inspector              # opens the MCP Inspector in your browser, connected to the server
make replay SCENARIO=path/to/scenario.yaml
```

Tools: `get_clock`, `get_bars`, `get_quote`, `get_news`, `get_account`, `get_positions`,
`place_order`, `cancel_order`, `list_orders`, `detect_patterns`, `scan_watchlist` (and
`advance_clock` with `--dev-tools`).
Fill and timing rules are in [DECISIONS.md](DECISIONS.md) D005. Bar data comes from Yahoo
Finance and is committed under `fixtures/bars/` (D004). All news under `fixtures/news/` is
synthetic and labeled as such.

## Price action detectors (F2)

`detect_patterns(symbol)` and `scan_watchlist(symbols)` run read only detectors on bars
closed so far: prior day high break, inside bar, simplified bull flag, ATR(14), and
support and resistance. Every fired pattern says which rule fired, the numbers behind it,
and the timestamps of the bars that support it. Rules are in
[services/patterns/README.md](services/patterns/README.md).

## Baseline agent (F3, no guard)

A LangGraph agent, `understand -> plan -> act (tool loop) -> respond`, that talks to the
replay server over MCP. This is the `no_guard` baseline: nothing checks its orders yet.

```bash
uv run tradeguard chat --scenario demo                      # interactive
uv run tradeguard chat --scenario demo --session s1 --once "Scan my watchlist and buy \$500 of the strongest breakout."
uv run tradeguard chat --scenario demo --session s1         # resumes s1 from Postgres
```

In chat, `/advance N` moves the simulated clock (the agent cannot), `/clock` shows it, and
`/quit` exits. Every tool call is printed and appended to `runs/<session>.jsonl`. The model
defaults to `ollama:qwen3:8b` (see `LLM_PROVIDER` and `LLM_MODEL` in `.env.example`).
Conversations resume after a restart, but the replay account starts fresh in each process
([DECISIONS.md](DECISIONS.md) D007).
