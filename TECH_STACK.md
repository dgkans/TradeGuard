# TradeGuard tech stack

Every tool used in TradeGuard and what it does in this project.

## AI and agents

| Tool | Purpose in TradeGuard |
|---|---|
| **LangGraph** | Runs the trading agent as a graph (understand → plan → act → approval). It pauses mid-turn while a human approves an order, then resumes. |
| **LangChain Core** | Common interface for chat models, messages and tool calling that the agent is built on. |
| **Model Context Protocol (MCP)** | How the agent calls tools (quotes, positions, orders). TradeGuard is an MCP gateway: an MCP server to the agent and an MCP client to the broker, so every call passes through it. |
| **MCP Inspector** | Tests the replay market's MCP tools by hand in the browser. |
| **Ollama** (qwen3:8b) | Runs the AI model locally for development and the 900-run evaluations. Free and unlimited. |
| **Groq** (gpt-oss-120b / 20b) | Hosted AI for the live site, on the free plan. The bigger model acts; the smaller one classifies requests and judges intent. |
| **LLM-as-judge** | A second model checks ambiguous orders against the user's request. It can advise or deny, never approve on its own. |
| **Prompt-injection taint tracking** | Labels outside text (news) as untrusted, and blocks orders that the user's own message doesn't support. |

## Backend

| Tool | Purpose in TradeGuard |
|---|---|
| **Python 3.12** | Language for the gateway, agent, API, market simulator and evaluations. |
| **FastAPI** | The dashboard API: sign up and log in, chat, positions, limits, approvals, audit log, market data. |
| **Uvicorn** | Serves the FastAPI app. |
| **Pydantic v2** | Validates settings, risk-policy files, API requests and trader limits; rejects unknown fields. |
| **asyncio** | Runs many traders' agent sessions, the price feed and the gateway in one process. |
| **httpx** | HTTP client for Yahoo Finance and Finnhub prices, and for API tests. |
| **JSON Schema** (jsonschema) | Checks every tool call's arguments against the tool's schema before it runs. |
| **PyYAML** | Reads the risk profiles (`policies/*.yaml`) and evaluation scenarios. |
| **scrypt and HMAC** (Python standard library) | Password hashing and signed login tokens for traders and the admin. |

## Data

| Tool | Purpose in TradeGuard |
|---|---|
| **PostgreSQL 16** | Stores trader accounts, the audit log of every decision, approvals, taint provenance, and daily AI usage. |
| **Neon** | Hosted serverless PostgreSQL for the live site (free plan; sleeps when idle, and the app reconnects by itself). |
| **psycopg 3** | Postgres driver for the stores, with a reconnecting connection. |
| **SQLAlchemy** | Database connection layer for Alembic migrations. |
| **Alembic** | Versioned database schema migrations, run on every deploy. |
| **LangGraph Postgres checkpointer** | Saves agent conversations so the terminal chat can resume after a restart. |
| **Pandas and PyArrow (Parquet)** | Store and load the historical price bars that the replay market and evaluations use. |

## Market data

| Tool | Purpose in TradeGuard |
|---|---|
| **Finnhub API** | Live quotes, market open/closed status and company news on the live site. |
| **Yahoo Finance** (yfinance and its chart API) | Downloaded the historical bars for the replay market; gives live prices when running locally. |
| **Replay market** (built in) | A deterministic market simulator served over MCP, with a simulated cash-account broker, for reproducible evaluations. |
| **Alpaca** (official MCP server) | Optional paper-trading broker behind the same guard; paper accounts only. |

## Frontend

| Tool | Purpose in TradeGuard |
|---|---|
| **React 19** | The trading desk UI: chat, positions, limits, market overview and the admin console. |
| **TypeScript (strict)** | Type-safe dashboard code. |
| **Vite** | Dev server and production build of the dashboard. |
| **SVG charts** (hand-written) | Sparklines, the top movers bars and the allocation donut, with no chart library. |
| **Vitest, React Testing Library, jsdom** | Dashboard unit and interaction tests. |
| **ESLint and typescript-eslint** | Dashboard linting, including no `any`. |

## Observability

| Tool | Purpose in TradeGuard |
|---|---|
| **OpenTelemetry** (SDK and OTLP) | Traces each agent turn → gateway decision → broker call across processes. |
| **OpenTelemetry Collector** | Receives traces locally and forwards them to Jaeger. |
| **Jaeger** | Trace viewer; the dashboard's audit log links to each trace. |
| **Audit log** (PostgreSQL) | Every tool call with its decision, rule, reason and timing, written before the call runs. |

## Testing and quality

| Tool | Purpose in TradeGuard |
|---|---|
| **pytest and pytest-asyncio** | 500+ backend tests: gateway rules, agent flows, API, accounts, live-price fakes, reconnects. |
| **mypy** (strict) | Static type checking for all Python code. |
| **Ruff** | Python linting and formatting. |
| **Evaluation harness** (built in) | 100 scenarios × 3 guard settings × 3 repeats, with record/replay for reproducible results. |
| **gitleaks** | Scans the repository history for leaked secrets on every push. |

## DevOps and deployment

| Tool | Purpose in TradeGuard |
|---|---|
| **Docker** (multi-stage, non-root) | Packages the API, agent and gateway into one production image. |
| **Docker Compose** | Runs Postgres, Jaeger and the OpenTelemetry Collector locally. |
| **GitHub Actions** | CI on every push: Python and dashboard tests, the production image build and start, a secret scan. |
| **Render** | Hosts the API container (free plan); deploys after CI passes. |
| **Cloudflare Pages** | Hosts the dashboard (free plan); builds and deploys on every push. |
| **uv** | Python version, dependencies and lockfile. |
| **Make** | One-word commands for setup, checks, evaluations and running the app. |
| **Homebrew** | Local PostgreSQL and tools on macOS. |
