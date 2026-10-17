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
                         #   (no Docker? brew Postgres + `make jaeger`, see below)
make migrate             # create audit_events and other tables
make check               # ruff, mypy, pytest (same as CI)
```

- Jaeger UI: http://localhost:16686
- OTLP: `localhost:4318` (HTTP, used by TradeGuard), `localhost:4317` (gRPC)
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

A LangGraph agent, `understand -> plan -> act (tool loop) -> respond`, that talks to its
tools over MCP. Since F4 it goes through the gateway by default. `--direct` skips the
gateway: that's the `no_guard` baseline.

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

## Gateway (F4, pass through)

```
tradeguard chat --stdio--> gateway (MCP server + client) --stdio--> replay server
                               |-> Postgres audit_events (written before forwarding)
                               '-> OTLP traces -> Jaeger
```

Every tool call is written to `audit_events` (session, tool, args, decision, rule,
timings, result summary, trace id) *before* it is forwarded. If that write fails, the call
is denied. F4 allows every known tool (`rule_id=pass_through`); the policy engine arrives
in F5.

```bash
make jaeger                                       # local Jaeger, no Docker needed
uv run tradeguard chat --scenario demo --session s1
uv run tradeguard gateway-stats --session s1      # p50 / p95 overhead from audit_events
make bench-gateway                                # direct vs gateway latency, JSON report
```

Traces show `agent.turn > agent.tool_call > gateway.decision > gateway.upstream_call > replay
tools/call` across the three processes. Measured overhead is in [RESULTS.md](RESULTS.md).
