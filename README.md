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

## Dashboard: trading desk and operator console (F10 to F10f)

React + TypeScript (strict) dashboard over the TradeGuard API. Needs Node 22 (20.19+).

```bash
make api                 # API on http://127.0.0.1:8000 (sessions, positions, approvals, audit, ...)
make dashboard-install   # once
make dashboard           # http://127.0.0.1:5173 (proxies /api to :8000)
make dashboard-check     # eslint (no `any`), tsc, vitest, production build
```

**Trade** (what a broker's customer sees):
- **Chat:** talk to a guarded AI agent in plain words. Each step shows as a sentence
  ("Checked NVDA: $212.74", "Order sent: buy $500 of AMD at market"). Blocked orders show
  as a notice with the reason, and large orders as an approval card. No JSON anywhere.
- **Positions:** a Kite style table (instrument, qty, avg, LTP, P&L, day change, totals),
  cash and holdings, and today's orders.
- **Trader accounts (F10e):** sign up with a username, password, risk profile and starting
  cash ($1,000 to $1,000,000, default $10,000), then log in and out. New accounts start
  with no positions. Accounts live in Postgres: positions, orders, limits and the chat
  come back after an API restart. A trader can only see and act on their own account.
  Passwords are scrypt hashed; login tokens are signed with `TRADER_TOKEN_SECRET` in
  `.env`.

- **My limits:** each trader edits their own limits within bounds:
  - per order and per stock;
  - share of account in stocks;
  - orders per day;
  - the approval line, and approval for large sales;
  - which stocks the agent may trade.

  Changes apply to the next order and are written to the audit log. The protections
  against the AI (did you ask for it, does it match) plus market hours and no shorting are
  always on.
- **Market view:** the landing page shows index and watchlist cards with sparklines and a
  top movers chart. A market watch panel sits beside the chat, and an allocation donut on
  Positions. All of it is drawn from live prices and the trader's account.

**Admin** (the owner only): log in on the same form with username `admin` and the
`ADMIN_PASSWORD` from `.env`. The owner gets **Traders** (every account and
who is online), **Approvals** (every trader), **Audit log**, **Guard activity** (what was stopped across traders, and the policy
files), and **Test results**. The API refuses these without the admin token. Tokens are
HMAC signed, expire after 12 hours, and die when the API restarts; repeated wrong
passwords lock the login for 5 minutes. Traders approve only their own orders, in chat.

The **How it works** button explains the flow (You → AI agent → TradeGuard → Broker) with
a worked example, and how a broker plugs it in.

**Live prices (F10f):** the desk is priced from Yahoo Finance's public chart data. There is
no account and no key. The API polls each symbol every `LIVE_POLL_SECONDS` (default 30)
while the market is open, and every 5 minutes while it's closed. The data comes with a
plain user agent and backs off on rate limits.
- Prices, sparklines, day change, P&L and the agent's tools (quotes, bars, patterns, real
  headlines as untrusted news) all use it.
- Orders fill only in TradeGuard's own simulated paper account, never at a real broker.
  During market hours a market order fills at the live price, and a limit order fills when
  the price reaches it.
- Outside market hours the guard blocks orders. The header shows "Live · Market closed ·
  opens Mon 9:30 AM ET", using Yahoo's own session times, which include holidays.
- If prices go stale, orders are refused and the header says so (fail closed).
- `LIVE_PRICES=false` switches the desk back to the replay fixtures.
- The eval harness always uses replay data, so its numbers stay reproducible.

Each trader's market and guard run inside the API process, which binds to 127.0.0.1.

### Two minute walkthrough

Start Postgres, Ollama, `make jaeger`, `make api` and `make dashboard`, then open
http://127.0.0.1:5173.

1. **Sign up (10 s).** Open **Sign up**, choose a username (e.g. asha) and a password,
   keep Standard and $10,000, then press **Create account**. Positions are empty.
2. **Ask and trade (40 s).** Tap the "What is NVDA at?" suggestion (a live price), then
   send "Buy $500 of AMD". Each step appears as a line, then the agent's answer. Orders
   need the market to be open (9:30 to 16:00 New York time on trading days); outside
   those hours they are blocked with "the market is closed".
3. **A blocked order (20 s).** Send "Buy $3000 of TSLA". A red notice says **Order is over
   your per-order limit**, with the reason.
4. **An approval (30 s).** Send "Buy $1500 of NVDA". An approval card appears ("Large order:
   needs your approval"). Press **Approve**: "Approved. The order was sent."
5. **Positions (15 s).** Open **Positions**: AMD and the extra NVDA are there, filled, with
   P&L and day change.
6. **A second trader (20 s).** **Log out**, sign up "ben" with the **Strict** profile in
   another browser (or after logging out), and trade. Positions stay separate.
7. **Restart (20 s).** Stop and start `make api`, then log in again: positions, orders,
   limits and chat are still there.
8. **Admin (15 s).** Log out, then log in with username `admin` and `ADMIN_PASSWORD` from
   `.env`. **Traders** lists accounts, **Approvals** covers every trader, and **Audit log**
   shows every check with a Trace link.

**The database:** Postgres 16 from Homebrew (`brew services start postgresql@16`), database,
user and password `tradeguard` on localhost:5432 (`DATABASE_URL` in `.env`). `make migrate`
creates the tables. Open it with
`psql "postgresql://tradeguard:tradeguard@localhost:5432/tradeguard"`; accounts are in
`traders`, decisions in `audit_events` and `approvals`.

## Deploy (F12): free, live, auto-deploying

| Piece | Host (free plan, no card) | What it runs |
|---|---|---|
| API, trader sessions, gateway, live prices | Render web service (`render.yaml`, `Dockerfile`) | One container, 512 MB; migrates on start |
| Postgres | Neon | Accounts, audit log, approvals; open it with psql, TablePlus or Neon's SQL editor |
| Dashboard | Cloudflare Pages | The static React build; it calls the API with `VITE_API_URL` |
| AI | Groq | `openai/gpt-oss-120b` acts, `openai/gpt-oss-20b` classifies and judges |

Every push to `main` deploys by itself:
- Render waits for GitHub CI (tests, the Docker image, a secret scan) to pass.
- Cloudflare runs the dashboard's lint, tests and build, then publishes.

Secrets live only in the Render dashboard, never in git or the client bundle. Production
refuses to start without `ADMIN_PASSWORD`, `TRADER_TOKEN_SECRET` and the Groq key.

**It sleeps when idle:**
- The API shares the owner's existing Render workspace and its 750 free hours a month, so
  it isn't kept awake.
- After 15 minutes without visitors it sleeps; the next visitor waits about a minute. The
  dashboard shows "Starting the server…" until it answers.
- Prices refresh only while it's awake. Accounts, positions and the daily AI counts are in
  Postgres, so nothing is lost.

**Free plan guard rails:**
- Groq allows about 200K tokens a day on the main model, so the site caps AI turns per day
  (30 in all, 10 per trader, in `render.yaml`) and shows "N AI requests left today". The
  counts are kept in Postgres.
- Rate limits and provider outages show a plain message, and a failed turn isn't counted.
- Sign ups are limited per address.
- Neon suspends an idle database; every store reconnects on its own.

### Owner steps (once)

1. **Groq:** sign up at console.groq.com and create an API key. Keep it to paste into
   Render, never into chat or git.
2. **Neon:** sign up at neon.tech and create a project in AWS US West (Oregon), Postgres 16.
   Under *Connect*, turn **Connection pooling off** and copy the connection string (it ends
   in `?sslmode=require`).
3. **Render:** in your existing account, connect GitHub, then *New → Blueprint* and pick
   `dgkans/TradeGuard`.
   Render reads `render.yaml` and asks for:
   - `DATABASE_URL`: the Neon string;
   - `GROQ_API_KEY`;
   - `ADMIN_PASSWORD`: choose a strong one;
   - `CORS_ORIGINS`: type `https://example.com` for now.

   Deploy. The API gets an address like `https://tradeguard-api.onrender.com`.
4. **Cloudflare Pages:** *Workers & Pages → Create → Pages → Connect to Git* and pick the
   repo. Use:
   - root directory `dashboard`;
   - build command `npm ci && npm run check`;
   - output directory `dist`;
   - environment variable `VITE_API_URL` set to the Render address.

   The site gets an address like `https://tradeguard.pages.dev`.
5. **Back in Render:** set `CORS_ORIGINS` to the Pages address. Render redeploys.

Don't add an uptime ping: kept awake, the API alone would use about all of the
workspace's 750 free hours, and Render then suspends every free service in it (including
other projects) until the next month.

Check it: open the Pages address, sign up, ask "What is NVDA at?", and log in as `admin`.
`https://<render address>/readyz` should show `"database":"ok","prices":"ok"`.

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
