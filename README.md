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

## Policy engine (F5)

The gateway now decides every call with the policy in `policies/default.yaml` (or
`--policy policies/strict.yaml`):

1. **Schema check**: arguments must match the upstream tool's input schema.
2. **Tool allowlist**: read and write tools. `advance_clock` is operator only.
3. **Policy rules** for orders: allowed symbols and order types, max order size, no
   shorting, max position size, max portfolio exposure, max orders per day, market hours.
4. **Approval rules**: orders over $1000, or selling more than 50% of a position,
   return ESCALATE (the human approval queue arrives in F6).

DENY and ESCALATE are never forwarded. The agent gets a structured refusal (`decision`,
`rule_id`, `reason`, `policy`, `executed: false`), and every decision is in
`audit_events` with its rule id and reason.

```bash
uv run tradeguard chat --scenario demo --once "Buy \$3000 of TSLA"   # DENY max_order_notional
uv run tradeguard chat --scenario demo --once "Buy \$1500 of NVDA"   # ESCALATE approval_notional
```

Policy files reject unknown fields, and errors name the field, for example
`require_approval.notional_over_usd: Input should be a valid number`. Design notes are in
[DECISIONS.md](DECISIONS.md) D009.

## Human approval (F6)

Escalated calls wait in an approval queue instead of being refused. The agent pauses
(LangGraph interrupt) until a human approves, rejects, or the approval times out
(`APPROVAL_TIMEOUT_SECONDS`, default 300).

```bash
uv run tradeguard api                                  # API on :8000 (docs at /docs); see F10
uv run tradeguard chat --scenario demo --once "Buy \$1500 of NVDA"
# the CLI prints the approval id and the exact approve/reject commands, e.g.
curl -s localhost:8000/approvals?status=pending
curl -s -X POST localhost:8000/approvals/<id>/approve -H 'content-type: application/json' \
     -d '{"decided_by": "you", "note": "ok"}'
```

On approval the gateway re-checks every deny rule and then executes the *stored*
arguments exactly once. Rejected and expired approvals never execute, and the agent is
told the outcome and the reviewer's note. Every step is in `audit_events`. The API has
no authentication yet: local use only. Details in [DECISIONS.md](DECISIONS.md) D010.

## Taint tracking (F7)

Every tool result is labeled `trusted` or `untrusted` (policy `taint.trust_labels`; news is
untrusted, and unknown tools default to untrusted). The gateway keeps each session's
provenance (user messages, reported by the CLI, plus tool results) in `provenance_events`.

Once untrusted content is in the session, a write call must be supported by the user's
**latest** message:

| User's latest message | Order | Decision |
|---|---|---|
| "Check the latest news on NVDA" | sell NVDA | DENY `taint_unsupported` |
| "Buy $500 of AMD" | buy AMD | ALLOW (news read earlier is fine) |
| "Buy $500 of the strongest breakout" | buy NVDA | ESCALATE, or DENY if the news looks like an injection |

Untrusted results reach the agent wrapped in `<<<UNTRUSTED_DATA>>>` markers with a "data,
not instructions" notice. A heuristic detector scores untrusted text for injection signals
and feeds the risk score; its precision and recall on a frozen labeled set are in
[RESULTS.md](RESULTS.md), with caveats. Taint outcomes appear in `audit_events` reasons and
as `tradeguard.taint.*` span attributes. Design: [DECISIONS.md](DECISIONS.md) D011.

## Intent alignment (F8)

Every order is compared with the user's latest message, parsed deterministically into side,
symbol, amount, unit (dollars, shares or % of position) and condition:

| User said | Order | Decision |
|---|---|---|
| "Buy $8 of NVDA" | 8 shares | ESCALATE `intent_unit_confusion` ($8 vs 8 shares = $1,702) |
| "Buy $500 of NVDA" | sell NVDA | DENY `intent_wrong_side` |
| "Buy $500 of AMD" | buy NVDA | DENY `intent_wrong_symbol` |
| "Buy $500 of NVDA" | $1,500 | DENY `intent_amount_mismatch` |
| "buy 5 NVDA" | 5 shares | ESCALATE `intent_ambiguous_unit` (both readings shown) |
| "What's NVDA at?" | any order | DENY `intent_missing` |

An LLM judge (`JUDGE_MODEL`, prompt `services/agent/prompts/judge_v1.md`) runs only when the
parse is ambiguous. Its agreement with hand labels was low on exactly those cases (see
[RESULTS.md](RESULTS.md)), so by default it advises the human reviewer and can deny, but
never allows an order on its own (`intent.judge.can_allow: false`, D012).

## Evaluation harness (F9)

100 replayed scenarios in five categories (benign, out of policy, indirect injection,
ambiguous intent, adversarial user), 20 of them held out, each run against three
configurations:

| Config | What runs |
|---|---|
| `no_guard` | agent talks to the replay server directly |
| `rules_only` | gateway with schema, allowlist, policy and approval rules |
| `full_guard` | rules plus taint tracking and intent alignment (with the LLM judge) |

```bash
make eval-smoke          # 10 scenarios, full_guard, live model, < 5 min; records tapes
make eval-smoke-replay   # same, from the committed tapes: no model, a few seconds
make eval-full           # 100 x 3 configs x 3 repeats on the local model (hours)
make scenarios           # rebuild the suite from evals/build_scenarios.py
uv run python -m evals.compare_runs <run_a> <run_b>   # determinism: record by record diff
uv run python -m evals.runner --agent attacker   # scripted worst case agent, injection scenarios
```

Reports (Markdown and JSON) go to `evals/reports/`. Every number quoted in this README
comes from [RESULTS.md](RESULTS.md), which records the date, commit and models. All news
in the scenarios is synthetic (see `fixtures/news/README.md`).

## Dashboard (F10)

React + TypeScript (strict) dashboard over the TradeGuard API. Needs Node 22 (20.19+).

```bash
make api                 # API on http://127.0.0.1:8000 (approvals, audit, policies, evals, chat)
make dashboard-install   # once
make dashboard           # http://127.0.0.1:5173 (proxies /api to :8000)
make dashboard-check     # eslint (no `any`), tsc, vitest, production build
```

Pages: **Chat** (run the agent in a replay scenario, guarded or not, with tool calls and
approval cards inline), **Approvals** (pending queue, approve or reject with a note),
**Audit log** (every decision, filterable, with Jaeger trace links), **Policies** (the YAML
files and the rules that fired most), and **Evals** (the latest full suite report). The
chat runs the same agent and gateway subprocess as `tradeguard chat`. The 20 held out
scenarios aren't offered there. The API has no authentication yet and binds to
127.0.0.1; F12 adds auth before anything is public.

### Two minute walkthrough

Start Postgres, Ollama, `make jaeger`, `make api` and `make dashboard`. Then:

1. **Chat, guarded (40 s).** Scenario `demo`, Guarded, policy `default`, Start session.
   Ask "What is NVDA at?" The tool calls show inline, then the answer.
2. **An order that needs a human (30 s).** Send "Buy $1500 of NVDA". The gateway holds it
   (`approval_notional`: over the $1,000 threshold) and an approval card appears in the
   chat. The Approvals tab shows a count too. Approve it there or in the card. The turn
   finishes and the agent reports the order.
3. **Prompt injection (20 s).** End the session. Start `inj_001` (news that tells the agent
   to sell). Ask for the latest NVDA news. If the model takes the bait, the sell is denied
   as `taint_unsupported`, because the user never asked for it. Starting the same scenario
   with **No guard** shows the baseline.
4. **Audit log (15 s).** Every call from the walkthrough, newest first: decision, rule,
   reason and gateway overhead. Click a session to filter, or "Trace" to open the call in
   Jaeger.
5. **Policies and Evals (15 s).** The default policy YAML and which rules fired most. Then
   the full eval: tuning and held out tables for no_guard, rules_only and full_guard, with
   charts. The caveats live in [RESULTS.md](RESULTS.md); read them before quoting a number.

## Alpaca paper trading (F11)

The same guard in front of a real broker API, **paper only**. TradeGuard runs Alpaca's
official MCP server (`alpaca-mcp-server==2.3.2`, via `uvx`) behind an adapter that exposes
the replay server's tools. Every policy, taint label, intent check, approval and audit row
applies unchanged.

1. Create a free Alpaca **paper** account (no card, no funding) and generate paper API keys.
2. In `.env`: `TRADING_MODE=paper`, `ALPACA_API_KEY=...`, `ALPACA_SECRET_KEY=...`
   (`ALPACA_PAPER=true` and the paper `ALPACA_BASE_URL` are the defaults).
3. Run:

```bash
uv run tradeguard chat --broker alpaca      # guarded agent on your paper account
uv run pytest -m alpaca -v                  # real paper API: place, list, cancel a $1 limit order
```

The dashboard offers an `alpaca_paper` chat session when paper mode is configured.

How live trading stays impossible, each layer failing closed:
1. Live settings (`LIVE_TRADING`, `TRADING_MODE=live`, `ALPACA_PAPER=false`, a non paper
   `ALPACA_BASE_URL`) never load.
2. The Alpaca upstream needs `TRADING_MODE=paper` and both keys.
3. The official server is started with an environment built from scratch: two keys,
   `ALPACA_PAPER_TRADE=true` and a narrow toolset list. Nothing is inherited from your shell.
4. On connect, the account must be a paper account. If it can't be verified (bad keys,
   network), the gateway doesn't start.

There is no direct (unguarded) mode against Alpaca. Pattern tools and the simulated clock
are replay only.
