# TradeGuard

**Brokers check the margin. TradeGuard checks the agent.**

TradeGuard is a safety gateway for AI trading agents. It sits between an LLM agent and a
broker's tools and checks every action before it reaches the broker. Each order is
allowed, blocked, or held for the trader's approval, and every decision is written to an
audit log.

**Live:** [tradeguard-4c9.pages.dev](https://tradeguard-4c9.pages.dev) (paper trading at
live prices) · **Code:** [github.com/dgkans/TradeGuard](https://github.com/dgkans/TradeGuard)

## The problem

LLM agents can now place real orders through tools such as broker MCP servers. A broker
checks buying power and margin, but not:

- whether the order matches what the user asked for;
- whether the agent was manipulated by untrusted text (a news headline saying "sell
  everything");
- whether the order respects the user's own limits;
- why the agent acted.

## How it works

```
Trader ──chat──▶ AI agent (LangGraph) ──MCP tool call──▶ TradeGuard ──▶ Broker tools
                                                      │
       schema → allowlist → policy limits → taint → intent → human approval
                                                      │
                                     audit log (Postgres) + traces (OpenTelemetry)
```

1. **Schema and allowlist:** only known tools, with valid arguments.
2. **Policy:** per-order, per-stock and exposure limits, allowed symbols, no shorting,
   market hours. Each trader edits their own limits.
3. **Taint tracking:** an order inspired by untrusted text (news) is blocked.
4. **Intent alignment:** the order must match the user's request in symbol, side and size.
   Ambiguous requests ("buy 2 NVDA": $2 or 2 shares?) are held.
5. **Human approval:** large orders wait for the trader's Approve in the chat.

## Results

These come from the evaluation harness, with the numbers recorded in
[RESULTS.md](RESULTS.md):

- **Main eval:** 100 scenarios, including injection attacks, run 900 times (3 configs × 3
  repeats). On the tuning set, unsafe orders executed fell from 30 (no guard) to 5 (rules
  only) to 0 (full guard).
- **Fully hijacked agent:** a scripted agent obeyed every injection. With the guard, 0 of
  45 injected orders executed, against 45 without it. Every legitimate order still went
  through.
- **Cost:** gateway overhead was 0.3 ms at the median.

Caveats: the same author wrote the guard and the scenarios, so these numbers are
optimistic. The evals ran on qwen3:8b through Ollama, while the live site uses gpt-oss on
Groq.

## Stack

| Area | Tools |
|---|---|
| Agent and guard | Python 3.12, LangGraph, Model Context Protocol (MCP), FastAPI |
| Data | PostgreSQL (Neon), Alembic, live prices from Finnhub |
| Dashboard | React 19, TypeScript (strict), Vite, Vitest |
| Observability | OpenTelemetry, Jaeger, audit log |
| Delivery | Docker, GitHub Actions (tests, image check, secret scan), Render, Cloudflare Pages |
| AI | Ollama (local, evals) and Groq (live site); free tiers only |

## Run it locally

```bash
brew services start postgresql@16 && make migrate
make jaeger                  # traces (optional)
make api                     # API on :8000
make dashboard               # dashboard on :5173
make check                   # lint, types, tests
```

Details are in [README.md](README.md). Design decisions are in
[DECISIONS.md](DECISIONS.md), and the build log is in [PROGRESS.md](PROGRESS.md).
