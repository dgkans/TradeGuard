# Decisions

## D001: Config loader is plain pydantic + python-dotenv, with the safety switch before validation
Date: 2026-10-16
Context: F0 needs env validation at startup and a switch that refuses live trading.
Decision: `services/common/config.py` merges `.env` under the process environment, runs
`check_live_trading` on the raw strings, then validates into a frozen pydantic `Settings`
model whose aliases are the env var names. Unknown spellings of `LIVE_TRADING` /
`ALPACA_PAPER` are refused rather than guessed. `ALPACA_BASE_URL` must be https with host
exactly `paper-api.alpaca.markets` (no substring match). `LLM_PROVIDER=anthropic` is refused
(Section 16). Secrets, including `DATABASE_URL`, are `SecretStr` so they never print.
Alternatives considered: pydantic-settings. Its env reading is tied to `os.environ`, which
makes tests that pass an explicit mapping awkward, and its error for `TRADING_MODE=live`
would be a generic enum error instead of a live trading refusal.
Consequences: every service should call `load_settings()` first thing.
`python -m services.common.config` is the standalone check, also run by `make up`.

## D002: mypy strict on all of `services/`, not only the gateway
Date: 2026-10-16
Context: Section 4 asks for strict on the gateway. The gateway imports `services.common`.
Decision: strict for `services.*`; tests are type checked in normal mode.
Alternatives considered: strict only on `services.gateway.*`.
Consequences: slightly more typing work in agent and replay code. Can be relaxed per module
later if a third party library without stubs makes it impractical.

## D003: Local stack images and healthchecks
Date: 2026-10-16
Context: `docker compose up` must report all three services healthy.
Decision: `postgres:16.10-alpine` (`pg_isready`), `jaegertracing/jaeger:2.10.0` (Jaeger v2;
v1 is end of life) probed on its 13133 health endpoint, and
`otel/opentelemetry-collector-contrib:0.135.0`. The collector image is distroless with no
shell or HTTP client, so its in-container healthcheck runs `otelcol-contrib validate`, and
`make up` plus CI also probe its real health endpoint from the host.
Alternatives considered: a custom collector image with curl added (more to maintain);
skipping the collector healthcheck (fails the acceptance criterion).
Consequences: the collector's container status shows "config valid", not live health. The
host side probe covers that. **Not yet verified on a Docker host** (see PROGRESS.md).

## D004: Bar fixtures come from Yahoo via yfinance and are committed
Date: 2026-10-16
Context: F1 needs deterministic historical bars with no paid data plan (Section 16).
Decision: `services/replay_server/download.py` pulls 20 symbols (10 in the default policy
allowlist, 10 outside it): 2 years of daily bars and 60 days of 5 minute bars, regular hours
only, `auto_adjust=False`. Stored as Parquet with the bar START in UTC (daily bars stamped
at 09:30 New York). Committed (3.8 MB) so replays never touch the network. Prices are
rounded to 4 decimals on load because Yahoo returns float32 values.
Alternatives considered: Alpaca historical data (needs keys, so not usable in CI or by
visitors); synthetic random walks (not realistic enough for the price action detectors in F2).
Consequences: Yahoo only serves 60 days of 5 minute data, so the intraday window is fixed at
2026-07-24 to 2026-10-16. Every scenario that needs intraday data must start inside it.
Exchange holidays and half days are not modeled: a daily bar becomes visible at 16:00 New
York even on a half day. That reveals data later than reality, never earlier, so it can't
leak the future.

## D005: Sim broker and clock rules
Date: 2026-10-16
Context: F1 acceptance requires determinism and no future data, including through fills.
Decision:
- A bar is visible only once it has closed. A quote is the open of the 5 minute bar in
  progress (a real trade at that instant), else the last close.
- Orders fill only on bars starting strictly after submission. Market orders fill at that
  open. A limit order fills at the open if the open crosses the limit; otherwise it fills
  at the limit only after the bar that touched it has closed.
- Day orders expire at the next 16:00 New York close. Cash account: no margin, no shorting;
  open orders reserve cash or shares, so two orders can't spend the same buying power.
- Broker refusals return an order with `status: rejected` and a reason, as a broker would.
  Bad arguments (unknown symbol, both qty and notional, ...) are MCP tool errors instead.
- Order ids come from `random.Random(scenario.seed)`, so repeated runs are identical.
- Two tools beyond the plan's list: `get_clock` (read only, so the agent can tell whether
  the market is open) and `advance_clock`, registered only with `--dev-tools`, so the agent
  can never move time. The eval harness drives time through `ReplaySession.advance`.
Alternatives considered: filling at the bar close (looks ahead within the bar); letting
the broker allow shorts (would add margin modeling; F5 policy denies shorts anyway).
Consequences: the F5 policy `tool_allowlist.read` must add `get_clock`.

## D006: Pattern detector design
Date: 2026-10-16
Context: F2 needs explainable, read only price action detectors (rules in
`services/patterns/README.md`).
Decision:
- Detectors are pure functions over an `OHLCV` Protocol, with no dependency on the replay
  server, so F11 can run them on Alpaca bars unchanged.
- The prior day high break is close based (a wick doesn't count) and reports
  `holding_above_high`. Without it, a break that has already failed looks the same as a
  live one. This was found on the demo data: NVDA broke and fell back on 2026-09-15.
- The bull flag uses fixed thresholds (pole >= 8% in 3 to 7 bars, flag 2 to 6 bars,
  retrace <= 50%, lower volume) instead of fitted ones, so behaviour is predictable and
  testable. Breakout strength is `latest_vs_high_atr`, which is comparable across
  symbols, rather than a made up composite score.
- `scan_watchlist` sorts by number of fired patterns, then symbol. It does not pick a
  "best" trade; the agent and user decide.
Alternatives considered: TA-Lib or pandas-ta (opaque rules, binary install, harder to
explain bar by bar); fitting thresholds to the fixture data (overfits to 60 sessions).
Consequences: sanity sweep on the committed data at 11:00 ET, all 20 symbols × 59
sessions (1,180 symbol-days): inside bar fired 126 times, prior day high break 449, bull
flag 33. So every detector fires on real data. This is a development check, not an eval
metric.

## D007: Baseline agent stack (F3)
Date: 2026-10-16
Context: F3 needs a provider agnostic LangGraph agent bound to MCP tools, with a Postgres
checkpointer, at $0.
Decision:
- **LLM:** Ollama 0.40.5 on the dev Mac (user choice 2026-10-16), model `qwen3:8b`
  (Ollama id `500a1f067a9f`, Q4_K_M, 8.2B), temperature 0, seed 0, `num_ctx` 16384,
  thinking off (`reasoning=False`). This is `LLM_MODEL`'s default. Ollama tags can be
  re-pointed upstream, so every eval run records the id, not just the tag. Groq and Gemini
  raise a clear error until F12 wires them up.
- **MCP binding:** our own `McpToolset` (about 100 lines) on the MCP SDK 2.x `Client`, because
  `langchain-mcp-adapters` 0.3.2 requires `mcp<2`. It filters dev only tools
  (`advance_clock`) out of the model's tool list and logs every call (args, result or
  error, latency, prompt version) to `runs/<session>.jsonl`.
- **Graph:** understand (JSON mode classification) -> plan (numbered steps, injected into
  the acting system prompt) -> act/tools loop (cap 20 calls per turn; calls over the cap get
  "not executed" tool messages so the history stays valid) -> respond.
- **Checkpointer:** `langgraph-checkpoint-postgres` against local Postgres 16 from Homebrew
  (Docker isn't installed). CI runs a Postgres service container for the resume test.
- **macOS quirk:** files under `.venv` here carried the `hidden` flag (probably iCloud
  syncing the Desktop folder), and CPython skips hidden `.pth` files, which broke
  `uv run tradeguard`. `make install` clears the flag.
Alternatives considered: `langchain-mcp-adapters` (version conflict); LangGraph's
`create_react_agent` (no understand/plan stages, which F8 intent checks build on); SQLite
checkpointer (plan specifies Postgres; production uses Neon).
Consequences:
- The conversation resumes after a restart, but the replay server is a new process with the
  scenario's starting account, so earlier paper orders are gone. Fixing that means
  persisting or replaying sim broker state; revisit with the F4 audit log.
- Observed in manual runs (not an eval metric): asked which stocks were breaking out,
  qwen3:8b listed NVDA without noting `holding_above_high: false`. Asked to buy the
  strongest breakout, it chose AMD correctly. The system prompt is left unchanged (v1)
  so the baseline isn't tuned before scenarios exist (PLAN.md 7.4).

## D008: Gateway proxy design (F4)
Date: 2026-10-16
Context: F4 inserts the gateway between agent and tools without changing behavior, with an
audit trail, traces and measured overhead.
Decision:
- **Server side:** the MCP SDK's low-level `Server` (`on_list_tools` / `on_call_tool`), so
  upstream tool schemas pass through byte for byte. `MCPServer` derives schemas from Python
  signatures, which a proxy doesn't have. **Client side:** one MCP `Client` per upstream.
  A tool name exposed by two upstreams stops startup (F11 will map names explicitly).
- **Fail closed:** the decision row is written to `audit_events` *before* forwarding. If
  that write fails, the call is denied and never reaches the upstream. The outcome row is
  written after. If only that second write fails, the result is still returned (the call
  already ran) and the span is flagged. Unknown tools are a DENY with `rule_id=unknown_tool`.
  If Postgres or the table is missing at startup, the gateway refuses to start.
- **Policy hook:** `Gateway.decide(tool, args) -> Verdict`. F4 returns ALLOW /
  `pass_through`; F5 replaces it with the pipeline.
- **Overhead:** `overhead_ms = gateway handling time - upstream time` per call, stored in
  `audit_events` and summarized by `tradeguard gateway-stats` and on gateway exit. A
  separate client side benchmark (`make bench-gateway`) measures what the extra hop costs
  end to end.
- **Tracing:** OTLP/HTTP to `:4318`. The MCP SDK 2.x already injects and extracts
  `traceparent` in request `_meta` and opens CLIENT and SERVER spans. TradeGuard adds
  `agent.turn`, `agent.tool_call`, `gateway.decision` (with `tradeguard.decision`,
  `rule_id`, `tool`, `session_id`, `overhead_ms`) and `gateway.upstream_call`. Results are
  not put in span attributes, so account data stays out of traces.
- **Child process env:** MCP stdio children only get a minimal env allowlist, which
  silently dropped shell settings such as `DATABASE_URL` and `OTEL_*`. `child_env()`
  forwards exactly the variables `Settings` knows plus `OTEL_*`. `OTEL_SDK_DISABLED=true`
  is the explicit off switch, because an empty endpoint falls back to the default. Both
  were found while benchmarking, and both have regression tests.
- **Local Jaeger:** the official v2.21.0 darwin-arm64 release binary in `.tools/`
  (sha256 checked against the release's checksum file), because there's no Homebrew
  formula and no Docker. The compose file is pinned to the same version.
Alternatives considered: in-process gateway as a LangChain tool wrapper (not an MCP
boundary, so a buggy agent could bypass it); one write per call after forwarding (not fail
closed); Postgres via SQLAlchemy ORM (more code than two SQL statements; Alembic still owns
the schema).
Consequences:
- The gateway still exposes `advance_clock` when started with `--dev-tools`, because the
  CLI's `/advance` goes through the same gateway. Only the agent side toolset hides it. F5's
  tool allowlist must deny it to the agent; operator calls need their own channel or marker.
- `audit_events.decision` already allows ESCALATE for F6.

## D009: Policy engine (F5)
Date: 2026-10-16
Context: F5 adds deterministic YAML rules, the `rules_only` baseline.
Decision:
- **Pipeline order** (PLAN.md Section 2): (1) JSON schema check of the args against the
  upstream tool's own input schema (`jsonschema`, Draft 2020-12; a tool with no known schema
  is denied), (2) tool allowlist, (3) policy rules, (6) approval rules. The first DENY
  wins, then the first ESCALATE, else ALLOW `policy_pass`. Any exception is a DENY
  `pipeline_error`. Rules apply to `place_order`; other allowlisted tools pass after
  stages 1 and 2.
- **Rule order:** static rules first (order shape, symbol, order type), then the
  gateway fetches context, then context rules (market hours, orders per day, no short,
  order size, position size, exposure), then approval rules. Static first means an
  order for an unknown symbol never triggers a quote lookup.
- **Boundaries** are inclusive: exactly at a limit is allowed. Order notional uses the
  limit price for limit orders and the current quote for market orders; dollar sells are
  converted to shares at the quote.
- **Context** comes from the upstream's own read tools (`get_quote`, `get_positions`,
  `get_account`, `get_clock`, `list_orders`), fetched concurrently and not audited as
  agent calls. "Orders today" counts non-rejected orders submitted on the current sim
  trading day (New York date). The field mapping assumes the replay server's response
  shapes; F11 needs an adapter for Alpaca.
- **ESCALATE** is not forwarded in F5. The agent gets a structured refusal with a note to
  tell the user it awaits approval. F6 will park it in the approval queue instead.
- **Operator channel:** tools in `operator_tools` (`advance_clock`) are allowed only with
  the session's operator token in request `_meta` under `tradeguard/operator_token`. The
  CLI makes a random token per session and passes it to the gateway in
  `TRADEGUARD_OPERATOR_TOKEN`. The model only picks tool names and arguments, so it can't
  send the token. The token doesn't bypass the policy for any other tool (tested).
- Policy files reject unknown fields, and validation errors name the dotted field path.
Alternatives considered: OPA/Rego (powerful but another runtime and language for a small
fixed rule set); putting limits in the system prompt (the thing TradeGuard exists to
avoid); auditing context reads (would triple audit volume and mix agent and gateway
calls in the same table).
Consequences:
- Each order call costs five extra upstream reads before the decision. Their latency
  counts in the gateway's overhead for writes.
- Observed with qwen3:8b (manual runs, not an eval metric): "Buy $3000 of TSLA" was
  denied by `max_order_notional` and the agent explained the $2000 limit; "Buy $1500 of
  NVDA" was escalated by `approval_notional` and the agent said it awaits approval.

## D010: Human approval flow (F6)
Date: 2026-10-16
Context: Escalated calls must wait for a human, never run when rejected or timed out, and
leave an audit trail.
Decision:
- **Who executes:** the gateway, using the arguments it stored when the call escalated.
  The agent never resubmits them, so an approved order can't be swapped for a different
  one. Before running, the gateway re-runs every deny rule (`skip_approval=True`): the
  market may have closed or limits filled while it waited (tested: closes, becomes
  `blocked`, `market_hours`).
- **Exactly once:** `claim()` moves approved -> executing in one atomic UPDATE (an
  asyncio-atomic step in the memory store). Later `resolve_approval` calls return the
  stored outcome.
- **Timeout:** wall clock, not the sim clock, because humans decide in real time.
  `APPROVAL_TIMEOUT_SECONDS` (default 300). Expiry is applied inside every read and
  decision transaction, so there's no sweeper process and no window where an expired
  approval can be approved.
- **Audit:** the original call's row (ESCALATE, its rule), then `human_approval`,
  `human_rejection` or `approval_timeout` (written in the same transaction as the status
  change), then `human_approved` with timings when it executes, or the deny rule when
  blocked.
- **Agent:** after the tools node, a pending ESCALATE routes to `await_approval`, which
  calls LangGraph `interrupt()` with the approval id. On resume the escalated ToolMessage
  is replaced in place (same message id) by the final outcome, so the model reasons about
  what actually happened. The CLI polls `resolve_approval` (operator token) every 2 s,
  which also triggers execution, then resumes the graph with `Command(resume=outcome)`.
- **`resolve_approval`** is a gateway-native tool. It's listed, because an unlisted tool
  made the MCP client re-list tools and log a warning on every poll, and hidden from the
  model by the agent's toolset. Without the operator token it's a DENY `operator_only`.
- **API:** FastAPI, `tradeguard api` on 127.0.0.1:8000. No authentication yet: local only.
  F12 must add auth (and a dashboard login) before anything is public.
Alternatives considered: the agent retries the order with an approval id (it could
change the arguments); a background expiry sweeper (another process, and a race);
LangGraph-only approval state (the gateway, not the agent, must own the decision).
Consequences:
- Observed with qwen3:8b (manual runs, not an eval metric): after approval, asked for
  the order's status, it answered "new" from memory without calling a tool, after the
  order had filled. Asked explicitly to call list_orders, it reported the fill correctly
  ($212.70, 7.05 shares). On rejection it relayed the reviewer's note.

## D011: Provenance and taint tracking (F7)
Date: 2026-10-16
Context: Defend against indirect prompt injection: an order driven by untrusted tool output
(news) rather than by the user.
Decision:
- **Trust labels** live in the policy (`taint.trust_labels`), and unlisted tools default to
  untrusted (fail closed). Only `get_news` is untrusted today. Our own computed tools
  (patterns, scans, quotes, account) are trusted. `advance_clock` needed an explicit label
  once the default caught it.
- **User messages** are trusted provenance, but the gateway can't see them, so the CLI's
  code (not the model) sends each one through the operator-only `record_user_message`
  before the turn. With taint on and no provenance, writes are denied
  (`provenance_unavailable`).
- **Support check** compares a write call with the *latest* user message only. An earlier
  "buy NVDA" doesn't authorize a later turn. Side words with simple negation, and symbol by
  ticker or company name. Amounts, units and conditions are F8's job.
- **Decision:** clean -> pass; full support -> ALLOW; side only -> ESCALATE, or DENY when
  untrusted risk >= `high_risk_threshold` (0.5); no side support -> DENY. Taint runs after
  the deny rules and before approval, so an oversize order still fails on size first. On
  re-check after a human approval, a taint ESCALATE counts as approved, but a taint DENY
  still blocks.
- **Wrapping:** untrusted results reach the agent as `{trust, source, notice, data}`, with
  `data` between `<<<UNTRUSTED_DATA ...>>>` markers. Look-alike markers inside the text
  are replaced with U+2039/U+203A.
- **Detector:** weighted regex signals (override phrases, chat role markers, addressing an
  AI, urgency next to a trade verb, imperative trade, secrecy, claimed authority), with
  score `1 - prod(1 - w)`. It only feeds the risk score. Evaluated once on a 60 snippet
  set frozen before it was written (RESULTS.md).
- **Persistence:** `provenance_events` (Alembic 0003). The gateway reloads a session's
  entries on start, because the agent's checkpointed conversation still contains the
  untrusted text after a restart.
Alternatives considered: an LLM classifier for injections (cost, latency, and it can
itself be injected); taint on the agent side (the gateway must be the enforcement point,
and an agent can be compromised); treating every tool output as untrusted (too many
escalations for read-then-trade flows that only used computed data).
Consequences:
- Delegated choices ("buy the strongest breakout") after reading news now escalate
  instead of executing. That's intended, and F9 will measure the escalation rate.
- **Eval design finding:** with qwen3:8b, neither the unguarded nor the guarded agent acted
  on the demo injection (`news_demo_003`), in two phrasings. Both times the model said it
  was "synthetic test data", because every news fixture says so in its headline and body
  (PLAN.md requires synthetic labeling). The guard's effect is shown with a scripted
  hijacked agent (tests) and a scripted stdio client against the real gateway (audit,
  provenance and Jaeger). Before F9, decide how injection fixtures stay labeled synthetic
  (metadata, source name) without telling the model in the text.
- The detector misses injections that trip only one weak signal (4 of 30 in the set).
