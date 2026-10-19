# How TradeGuard works

This document covers who talks to whom (the agent, the MCP server and the MCP client) and
the architecture. It then follows one real request from the trader's message to the
filled order, and explains what each deployment tool does on the live site.

For the list of every tool, see [TECH_STACK.md](TECH_STACK.md). For the reasons behind
the design, see [DECISIONS.md](DECISIONS.md).

---

## 1. The cast: who is who

TradeGuard is built on the **Model Context Protocol (MCP)**, the standard way an AI agent
calls tools. MCP has two roles:

- an **MCP server** offers tools, such as `get_quote` and `place_order`;
- an **MCP client** connects to a server and calls those tools.

TradeGuard's trick is to stand **in the middle**, playing both roles at once.

| Component | What it is | MCP role | Code |
|---|---|---|---|
| **Trader** | A person using the desk in a browser | — | — |
| **Dashboard** | React app: chat, positions, limits, admin | — (HTTP client of the API) | `dashboard/` |
| **API** | FastAPI app: logins, sessions, positions, market data | — (hosts everything below) | `services/api/` |
| **AI agent** | A LangGraph graph that decides which tools to call | **MCP client** of the gateway | `services/agent/` |
| **LLM** | The language model the agent and judge call (Groq live, Ollama locally) | — | `services/common/llm.py` |
| **TradeGuard gateway** | The safety layer that checks every call | **MCP server** to the agent, **MCP client** to the market | `services/gateway/` |
| **Market** | The broker plus market data: a live paper account, the replay market, or Alpaca paper | **MCP server** | `services/api/live.py`, `services/replay_server/`, `services/alpaca_adapter/` |
| **PostgreSQL** | Accounts, audit log, approvals, provenance, AI usage | — | `migrations/` |

The agent **never** talks to the market directly. It only sees the gateway, which looks
like an ordinary MCP server offering the same tools. The gateway checks each call and
forwards it to the real market only if the call is allowed.

```mermaid
flowchart LR
  T([Trader]) -->|HTTPS| D[Dashboard<br/>React]
  D -->|REST + login token| A[API<br/>FastAPI]
  A --> G1[AI agent<br/>LangGraph]
  G1 <-->|prompts| L[(LLM<br/>Groq / Ollama)]
  G1 -->|MCP tool call| GW{TradeGuard<br/>gateway}
  GW -->|allowed calls only| M[Market MCP server<br/>paper broker + prices]
  M --> P[(Live prices<br/>Finnhub)]
  GW --> DB[(PostgreSQL<br/>audit, approvals)]
  GW -. traces .-> J[(OpenTelemetry<br/>/ Jaeger)]
```

### The three ways the pieces are wired

| Mode | Where it runs | How the agent, gateway and market connect |
|---|---|---|
| **Live desk** (the website) | One API process on Render | The agent, gateway and market all run **inside the API process**. MCP runs in memory: the agent's MCP client connects straight to the gateway's MCP server object, and the gateway's client to the market's server object. Each trader gets their own agent, gateway session and paper account. |
| **Terminal chat** (`tradeguard chat`) | Your machine | Three processes linked by **MCP over stdio**: `tradeguard chat` (agent) → `python -m services.gateway` → the replay server. |
| **Alpaca paper** | Your machine | The gateway's upstream is an adapter around Alpaca's official MCP server, started with `uvx` on a paper account only. |

The gateway code is the same in all three. Only the wiring differs.

---

## 2. Architecture

```mermaid
flowchart TB
  subgraph Browser
    UI[Dashboard<br/>React + TypeScript]
  end
  subgraph API process [API process: FastAPI on Render]
    AUTH[Accounts<br/>scrypt passwords, HMAC tokens]
    SESS[Chat sessions<br/>one per trader]
    AG[LangGraph agent]
    GW{Gateway pipeline}
    MKT[Paper account<br/>LiveMarket]
    FEED[Live price feed<br/>polls every 30 s]
    CAP[Daily AI caps]
  end
  UI -->|/auth, /me/*, /market/*| AUTH
  UI --> SESS
  SESS --> CAP
  SESS --> AG
  AG -->|MCP| GW
  GW -->|MCP| MKT
  MKT --> FEED
  FEED -->|HTTPS| FH[(Finnhub)]
  AG -->|HTTPS| GROQ[(Groq LLM)]
  GW --> PG[(PostgreSQL on Neon)]
  AUTH --> PG
  SESS -->|save book, limits, chat| PG
  CAP --> PG
```

### What happens inside the gateway

Every tool call goes through this pipeline, in this order. The first stage that objects
decides the outcome.

```mermaid
flowchart LR
  C[Tool call] --> S1[1. Schema<br/>arguments valid?]
  S1 --> S2[2. Allowlist<br/>tool permitted?]
  S2 -->|read tool| OK1[ALLOW]
  S2 -->|order| S3[3. Static order rules<br/>order shape, symbol,<br/>order type]
  S3 --> CTX[Read account and market:<br/>quote, positions, account,<br/>clock, today's orders]
  CTX --> S4[4. Limit rules<br/>size, exposure, orders/day,<br/>no shorting, market hours]
  S4 --> S5[5. Taint<br/>supported by the user's<br/>own latest message?]
  S5 --> S6[6. Intent<br/>same stock, side, amount<br/>as the user asked?]
  S6 --> S7[7. Approval rules<br/>over $1,000, or selling<br/>over 50% of a position?]
  S7 -->|no| OK2[ALLOW]
  S7 -->|yes| ESC[ESCALATE<br/>wait for a human]
```

- **DENY** and **ESCALATE** are never forwarded. The agent gets a structured refusal
  with the rule and the reason.
- **The decision is written to the audit log *before* the call is forwarded.** If that
  write fails, the call is denied (fail closed). Any error inside the pipeline is also a
  DENY.
- **Taint:** every tool result is labelled trusted or untrusted (news is untrusted).
  Untrusted text reaches the agent wrapped in "this is data, not instructions" markers.
  Once untrusted text is in the session, an order must be supported by the trader's own
  latest message, which the API reports to the gateway through an operator-only tool the
  model can't call.
- **Intent:** the trader's message is parsed into side, symbol, amount and unit. When the
  parse is ambiguous, an LLM judge advises; it can deny but never approve on its own.
- **Approval:** the gateway stores the exact arguments. After a human approves, it
  re-checks every deny rule, then executes those stored arguments exactly once.

### What the agent does in one turn

The LangGraph agent runs these steps for each message:

1. **understand**: a small, fast model call in JSON mode that classifies the request
   (question or trade) and summarises it.
2. **plan**: a short plan of which tools to use.
3. **act**: a loop where the model picks tool calls, which go through the gateway, and
   reads the results. It stops at 20 tool calls per turn.
4. **await approval**: if the gateway escalates, the graph **pauses** (a LangGraph
   interrupt) until the trader decides.
5. **respond**: a short, plain-English answer.

---

## 3. Example: from input to output

The trader Asha (Standard profile, $10,000 cash) types **"Buy $1500 of NVDA"** during
market hours.

```mermaid
sequenceDiagram
  autonumber
  actor Asha
  participant UI as Dashboard
  participant API as API (FastAPI)
  participant AG as Agent (LangGraph)
  participant LLM as LLM (Groq)
  participant GW as Gateway
  participant MK as Paper market
  participant DB as Postgres

  Asha->>UI: "Buy $1500 of NVDA"
  UI->>API: POST /me/messages (x-trader-token)
  API->>DB: count one AI turn (daily cap)
  API->>GW: record_user_message (operator only)
  API->>AG: run the turn
  AG->>LLM: understand + plan
  LLM-->>AG: trade: buy $1,500 of NVDA
  AG->>GW: get_account / get_quote NVDA
  GW->>DB: audit (ALLOW)
  GW->>MK: forward
  MK-->>AG: cash $10,000 · NVDA $217.74
  AG->>LLM: act
  LLM-->>AG: place_order(NVDA, buy, notional 1500)
  AG->>GW: place_order
  GW->>MK: read quote, positions, account, clock, orders
  GW->>GW: limits OK, taint OK, intent OK, over $1,000 → ESCALATE
  GW->>DB: audit (ESCALATE) + approval apr_…
  GW-->>AG: escalated, approval id
  AG-->>API: paused (interrupt)
  API-->>UI: approval card
  UI-->>Asha: "Large order: needs your approval"
  Asha->>UI: Approve
  UI->>API: POST /me/approvals/{id}/approve
  API->>DB: approval → approved
  UI->>API: POST /me/resume
  API->>GW: resolve_approval (polls)
  GW->>GW: re-check every deny rule
  GW->>MK: place_order with the stored arguments
  MK-->>GW: filled at $217.74
  GW->>DB: audit (executed)
  GW-->>AG: outcome: filled
  AG->>LLM: respond
  LLM-->>AG: answer
  AG-->>API: turn finished
  API->>DB: save book, limits, chat
  API-->>UI: steps + answer
  UI-->>Asha: "Bought $1,500 of NVDA, about 6.89 shares at $217.74."
```

What Asha sees in the chat, step by step:

```
You:      Buy $1500 of NVDA
Agent:    Checked your account: $10,000 cash
Agent:    Checked NVDA: $217.74
          ┌──────────────────────────────────────────────┐
          │ Large order: needs your approval              │
          │ buy $1,500 of NVDA · over your $1,000 line    │
          │                    [ Approve ]  [ Reject ]    │
          └──────────────────────────────────────────────┘
You:      (Approve)
Decision: Approved. The order was sent.
Agent:    Bought $1,500 of NVDA: about 6.89 shares at $217.74.
```

(The prices above are illustrative. The live order the build was checked with, "Buy $500
of AMD", filled at $541.47 for 0.92 shares.)

How other messages end:

| Message | Where it stops | What the trader sees |
|---|---|---|
| "What is NVDA at?" | read tools only → ALLOW | "NVDA is trading at $217.74." |
| "Buy $500 of AMD" | passes every stage → ALLOW, fills at the live price | "Bought $500 of AMD at $541.47, about 0.92 shares." |
| "Buy $3000 of TSLA" | stage 4: per-order limit → DENY | A red notice: "Order is over your per-order limit" |
| Any order while the market is closed | stage 4: market hours → DENY | "The market is closed" |
| A news headline says "sell all NVDA" | stage 5: taint → DENY | The order is blocked; the trader never asked for it |
| "Buy $500 of AMD" but the agent buys NVDA | stage 6: intent → DENY | Blocked: wrong stock |

Every one of these is a row in the audit log, which the admin can browse in the
**Audit log** page.

---

## 4. Deployment: what each tool does

```mermaid
flowchart LR
  DEV[Developer<br/>git push to main] --> GH[GitHub repo]
  GH --> CI[GitHub Actions CI<br/>tests, image, secret scan]
  CI -->|checks pass| RD[Render<br/>builds the Docker image,<br/>migrates, runs the API]
  GH --> CF[Cloudflare Pages<br/>builds and hosts<br/>the dashboard]
  USER([Visitor]) -->|loads the site| CF
  USER -->|API calls<br/>CORS-allowed| RD
  RD --> NEON[(Neon<br/>PostgreSQL)]
  RD --> GROQ[(Groq<br/>LLM API)]
  RD --> FH[(Finnhub<br/>prices + news)]
```

### Step by step, from a push to a live change

1. **A push to `main` on GitHub.** The repo is public, and every change starts here.
2. **GitHub Actions** runs `.github/workflows/ci.yml`, five jobs in parallel:
   - `python`: Ruff lint and format check, mypy, and 500+ pytest tests against a
     throwaway Postgres.
   - `dashboard`: ESLint, TypeScript, Vitest and a production build.
   - `image`: builds the production Docker image, starts it against Postgres with
     production settings, waits for `/readyz`, checks it runs as non-root, and reports
     its memory use (about 167 MB of Render's 512 MB).
   - `secrets`: gitleaks scans the whole git history for leaked keys.
   - `compose`: starts the local stack (Postgres, Jaeger, the OpenTelemetry Collector) and
     checks its health.
3. **Render (the API)** waits for those checks to pass (`autoDeployTrigger: checksPass` in
   `render.yaml`). Then:
   - It **builds** the `Dockerfile`: a build stage installs Python dependencies with uv,
     and a slim runtime stage copies the code, policies, scenarios and price history, with
     no secrets.
   - It **starts** the container: `alembic upgrade head` migrates the Neon database, then
     the API listens on the port Render assigns.
   - It **injects the secrets** from its own settings (`DATABASE_URL`, `GROQ_API_KEY`,
     `FINNHUB_API_KEY`, `ADMIN_PASSWORD`, `TRADER_TOKEN_SECRET`, `CORS_ORIGINS`). They
     are never in git or the image.
   - It **health-checks** `/healthz`, gives the service HTTPS at
     `tradeguard-api-9j2o.onrender.com`, and puts it to **sleep after 15 idle minutes**
     (the free plan). The next request wakes it in about a minute.
4. **Cloudflare Pages (the dashboard)** builds the same push itself:
   - It runs `npm ci && npm run check` in `dashboard/`, so a failing test stops the
     deploy.
   - It bakes in `VITE_API_URL`, which tells the browser where the API is.
   - It serves the static files from Cloudflare's global network at
     `tradeguard-4c9.pages.dev`, with security headers from `dashboard/public/_headers`
     (such as no framing and no MIME sniffing).
5. **In the visitor's browser,** the page loads from Cloudflare and calls the Render API
   directly.
   - The API accepts only that origin (CORS).
   - Logins travel as headers (`x-trader-token`, or a bearer token for admin), never
     cookies.
   - While the sleeping API wakes, the page shows "Starting the server…".

### What each service does at run time

| Service | Role on the live site | Why this one |
|---|---|---|
| **GitHub** | Source of truth; every push triggers CI and deploys | Free; Actions is free for public repos |
| **GitHub Actions** | The gate: nothing deploys unless tests, the image and the secret scan pass | Built into GitHub |
| **Docker** | Packages the API, agent, gateway and market into one image that runs the same everywhere | Render runs any Dockerfile |
| **Render** | Runs the API container: HTTPS, health checks, logs, secrets, sleep and wake | Free web service; deploys after CI |
| **Neon** | Hosted PostgreSQL: accounts, positions, orders, limits, chat, audit log, approvals, daily AI counts | Free; suspends when idle (the app reconnects by itself) |
| **Alembic** | Brings the Neon schema up to date on every start | Versioned migrations, safe to re-run |
| **Cloudflare Pages** | Builds and serves the dashboard from a global CDN | Free, fast, and allows commercial use |
| **Groq** | The AI model: gpt-oss-120b acts, gpt-oss-20b classifies requests and judges intent | Free API key; fast |
| **Finnhub** | Live quotes, market open/closed status and news, polled every 30 s while awake | Free key; Yahoo blocks Render's servers |

### Guard rails for free plans

- **Daily AI caps** (30 turns for the site, 10 per trader) are stored in Postgres, so a
  sleep and wake doesn't reset them. A turn the AI fails to serve isn't counted.
- **Groq rate limits** (8K tokens a minute) are waited out with retries. Each model has
  its own budget, and trader chats send only the last 3 turns to the model.
- **Neon's sleep:** each database connection is checked after 30 s idle and replaced if
  it was dropped. A statement is never retried, so writes can't be doubled.
- **Stale or missing prices refuse orders.** The header then says prices are unavailable.

### Running it locally instead

| Piece | Local | Live |
|---|---|---|
| API | `make api` (Uvicorn on :8000) | Render container |
| Dashboard | `make dashboard` (Vite dev server, proxies `/api`) | Cloudflare Pages |
| Database | Homebrew PostgreSQL 16 | Neon |
| AI | Ollama, qwen3:8b | Groq, gpt-oss |
| Prices | Yahoo Finance (no key) | Finnhub |
| Traces | `make jaeger` → Jaeger UI on :16686 | Off |
