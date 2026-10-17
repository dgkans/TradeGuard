# Progress

| Feature | Status | Date done |
|---|---|---|
| F0 Skeleton | done (compose + CI checks deferred to first push) | 2026-10-16 |
| F1 Replay server | done | 2026-10-16 |
| F2 Pattern detectors | done | 2026-10-16 |
| F3 Baseline agent | done | 2026-10-16 |
| F4 Gateway proxy | done | 2026-10-16 |
| F5 Policy engine | done | 2026-10-16 |
| F6 Approval flow | done | 2026-10-16 |
| F7 Taint tracking | done | 2026-10-16 |
| F8 Intent alignment | not started | |
| F9 Eval harness | not started | |
| F10 Dashboard | not started | |
| F11 Alpaca paper | not started | |
| F12 Deployment | not started | |
| F13 Impact and beta | not started | |
| F14 Ship it | not started | |

## F0 Skeleton
### Plan
- Create the Section 5 layout (empty packages and `.gitkeep` placeholders).
- `pyproject.toml` managed by uv, Python 3.12 pinned, lockfile committed. Dev tools: ruff, mypy (strict on `services/`), pytest, pytest-asyncio.
- `services/common/config.py`: pydantic + python-dotenv loader (D001) that validates every env var at startup and names the bad field.
- Safety switch inside the loader: refuse to start if `LIVE_TRADING` is true, `TRADING_MODE` is not `replay`/`paper`, `ALPACA_PAPER` is false, `ALPACA_BASE_URL` is not the Alpaca paper host, or `LLM_PROVIDER=anthropic` (Section 16).
- `python -m services.common.config` checks the env and exits non zero with a clear message.
- `.env.example` documenting every variable.
- `docker-compose.yml`: Postgres, Jaeger, OTel Collector, each with a healthcheck; `make up` waits for healthy.
- `Makefile`: `up`, `down`, `test`, `lint`, `typecheck`, `check`.
- `.github/workflows/ci.yml`: ruff, mypy, pytest on every push.
### Acceptance
- [ ] `docker compose up` starts Postgres, Jaeger and Collector with healthy checks
- [x] `uv run pytest` passes with a placeholder test
- [ ] CI passes on push
- [x] Startup fails with a clear error when live mode is configured (tested)
### Notes and open questions
- Docker is not installed on the dev Mac, so the compose stack has not been started yet. Needs Docker Desktop or OrbStack (both free for personal use).
- CI needs a push to GitHub (`dgkans/TradeGuard`) to verify. User decision 2026-10-16: push only at the end of the build, so this and the compose check (CI runs it too) are deferred, not blocking.
### Loop summary
- Built: repo layout, uv project (Python 3.12, lockfile), ruff/mypy/pytest config, env loader with live trading safety switch, `.env.example`, compose stack (Postgres, Jaeger v2, OTel Collector), Makefile, CI workflow (Python job + compose health job).
- Tests: 19 passing locally; ruff and mypy strict clean.
- Evals: none (not applicable to F0).
- Gaps: compose stack not started (no Docker on dev Mac); CI not yet run (not pushed). Deferred to the first push (user decision).
- Next: F1 Replay MCP server.

## F1 Replay MCP server
### Plan
- `download.py`: fetch 1d (2y) and 5m (60d, Yahoo's limit) bars for 20 symbols into `fixtures/bars/<interval>/<SYM>.parquet`; commit the files.
- `bars.py`: bar store; every timestamp is the bar start in UTC; a bar is visible only after it closes.
- `scenario.py`: YAML scenario model and loader (`--scenario demo` finds `scenarios/**/demo.yaml`).
- `news.py`: synthetic news fixtures, `trusted: false`; the eval-only `injection` field is stripped before anything reaches the agent.
- `broker.py`: cash account sim broker. Market/limit orders by qty or notional, fill at next 5m bar open (limit fills intra bar only once that bar has closed), day orders expire at the close, buying power and position checks, seeded order ids.
- `session.py`: one replay session = clock + broker + stores; implements the 8 tools plus read only `get_clock`; `advance()` moves time and processes fills.
- `server.py` / `__main__.py`: MCP SDK v2 `MCPServer`, stdio and streamable HTTP; `advance_clock` tool only with `--dev-tools`.
- Tests: determinism, no future data (bars, quotes, news, fills), buying power rejection, both transports.
### Acceptance
- [x] Same scenario seed returns identical data and fills on repeated runs (tested)
- [x] No tool can return data from the future relative to the sim clock (tested)
- [x] Sim broker rejects orders exceeding buying power (tested)
- [x] Server runs over stdio and streamable HTTP (tested; both transports spawned as subprocesses)
### Notes and open questions
- Yahoo serves 5m bars only for the last 60 days, so intraday scenarios must start between 2026-07-24 and 2026-10-16. Fixtures are committed, so this window is fixed from now on.
- Found during the demo run and fixed with regression tests: float32 price noise from Yahoo; a market order placed exactly on a bar boundary filled at that same bar's open instead of the next one.
- F5 must add `get_clock` to the read allowlist (D005).
### Loop summary
- Built: replay MCP server (MCP SDK 2.3 `MCPServer`) with 9 agent tools + dev-only `advance_clock`, bar store, sim clock, cash account sim broker, scenario loader, demo scenario, 4 synthetic news items (1 injection), committed bar fixtures for 20 symbols.
- Tests: 55 passing (36 new for F1); ruff and mypy strict clean.
- Evals: none (smoke eval starts once the agent exists).
- Gaps: no holiday calendar (D004); intraday scenarios limited to 2026-07-24..2026-10-16; F0 compose/CI checks still deferred to the first push.
- Next: F2 Price action pattern detectors.

## F2 Price action pattern detectors
### Plan
- `services/patterns/` holds pure functions over any bar with `start, open, high, low, close, volume` (a Protocol, so it has no dependency on the replay server).
- `atr(bars, n=14)`: Wilder ATR. `None` when there are too few bars.
- `inside_bar(bars)`: last daily bar strictly inside the one before it.
- `prior_day_high_break(prior_day, today_bars)`: first closed 5m bar today that *closes* above yesterday's high (a wick alone doesn't count).
- `bull_flag(bars)`: simplified rules: pole of 3 to 7 bars gaining at least 8%, then a flag of 2 to 6 bars with no new high, a retrace of at most 50% of the pole, highs drifting flat or down, and lower average volume than the pole.
- `support_resistance(bars)`: swing pivots (2 bars each side) clustered within 0.5 ATR, split around the last close, nearest first, with touch counts.
- Every detection returns the rule id, a plain English rule, the numbers that satisfied it, and the supporting bars by role and timestamp.
- Replay tools `detect_patterns(symbol)` and `scan_watchlist(symbols)` use only bars closed at the sim clock's "now".
- Tests: hand built bar sequences, positive and negative cases, boundaries; tool tests check that no output timestamp is after now.
### Acceptance
- [x] Each detector has unit tests on hand built bar sequences, including negative cases
- [x] Output explains which rule fired with bar timestamps
- [x] Rules documented in `services/patterns/README.md`
### Notes and open questions
- Demo run showed NVDA's prior day high break had already failed by 10:30, with nothing saying so. Added `holding_above_high` and a `latest_bar` reference (regression test).
- Sanity sweep (dev check, not an eval metric): 1,180 symbol-days, inside bar 126, prior day high break 449, bull flag 33 (D006).
### Loop summary
- Built: `services/patterns/` (ATR, inside bar, prior day high break, simplified bull flag, support/resistance, analyze/summarize) and replay tools `detect_patterns` and `scan_watchlist`.
- Tests: 84 passing (29 new for F2); ruff and mypy strict clean.
- Evals: none (the agent doesn't exist yet).
- Gaps: thresholds are fixed, not tuned; levels use daily bars only; the bull flag is daily only, no intraday flags.
- Next: F3 Baseline LangGraph agent (no guard).

## F3 Baseline LangGraph agent (no guard)
### Plan
- LLM: Ollama on the Mac (user choice 2026-10-16), model `qwen3:8b`, temperature 0, thinking off. `services/agent/llm.py` builds the chat model from `Settings`; groq/gemini give a clear "not wired yet" error until F12.
- `services/agent/tools.py`: our own MCP-to-LangChain binding on the MCP SDK 2.x `Client` (`langchain-mcp-adapters` needs `mcp<2`). It hides dev only tools (`advance_clock`) from the model, and logs every call with arguments, result, error and latency to `runs/<thread>.jsonl`.
- `services/agent/graph.py`: StateGraph `understand` (JSON: read_only / trade / other + summary) -> `plan` (short numbered steps) -> `act` (tool loop, max 20 tool calls) -> `respond`.
- Prompts versioned in `services/agent/prompts/` (`system_v1.md`); the version is recorded in state and in the tool log.
- Postgres checkpointer (`langgraph-checkpoint-postgres`) keyed by `--session`. Local Postgres from Homebrew (no Docker); CI gets a Postgres service.
- CLI: `uv run tradeguard chat --scenario demo [--session ID] [--once "..."]`, with `/advance N` to move the sim clock (the agent itself can't).
- Tests use a scripted fake chat model and an in-process replay server: read only answer, explicit order placed, checkpoint resume across new graph and connection, tool log contents, tool call cap, dev tools hidden.
### Acceptance
- [x] Agent answers read only questions using tools (tested with a scripted model; verified with qwen3:8b)
- [x] Agent places a paper order on an explicit instruction (tested; verified with qwen3:8b)
- [x] Session resumes after process restart (checkpoint tested against Postgres; verified across two CLI processes)
- [x] Every run logs tool calls with arguments and results (`runs/<session>.jsonl`, tested)
### Notes and open questions
- The conversation resumes from the checkpoint, but the replay server's sim broker state restarts from the scenario on a new process. See D007.
- Installed with Homebrew on the dev Mac: Ollama 0.40.5 + qwen3:8b, PostgreSQL 16 (role and database `tradeguard`), both running as brew services.
- Smoke eval (Section 7.5) can't run until F9 builds the harness.
- Manual runs with qwen3:8b: each turn took about 21 to 24 s locally (wall clock, including process start).
### Loop summary
- Built: LangGraph agent (understand/plan/act/respond), Ollama chat model factory, our own MCP-to-LangChain tool binding with JSONL call log, versioned prompts, Postgres checkpointer, `tradeguard chat` CLI with `/advance`.
- Tests: 92 passing (8 new for F3, including the Postgres resume test); ruff and mypy strict clean.
- Evals: none (harness is F9).
- Gaps: replay account resets per process; groq/gemini not wired; baseline missed a failed breakout once in manual testing (D007).
- Next: F4 Gateway proxy (pass through).

## F4 Gateway proxy (pass through)
### Plan
- `services/gateway/mcp_proxy/`: the gateway is a low-level MCP SDK `Server` (upstream tool schemas pass through unchanged) and an MCP `Client` to each upstream. It lists the union of upstream tools and refuses to start on a name collision (fail closed).
- Every call: write an `audit_events` row with decision ALLOW (pass through) *before* forwarding. If that write fails, deny and don't forward. After the upstream returns, update the row with latency, overhead, error flag and a result summary.
- `audit_events` table via Alembic migration: session, upstream, tool, args (jsonb), decision, rule_id, reason, timings, is_error, result_summary, trace and span ids.
- Telemetry: `services/common/telemetry.py` sets up an OTLP/HTTP exporter. The MCP SDK already propagates `traceparent` through request `_meta`. Add spans `agent.turn`, `gateway.decision` (attributes `tradeguard.decision`, tool, session) and `gateway.upstream_call`.
- Process layout: CLI -> stdio -> gateway -> stdio -> replay server. `tradeguard chat --direct` keeps the F3 no guard path for evals.
- Overhead: `overhead_ms` = gateway time minus upstream time, per call. p50 and p95 from `audit_events` via `tradeguard gateway-stats`, plus a client side benchmark (direct vs through gateway) recorded in RESULTS.md.
- Local Jaeger 2.21.0 binary (no Docker), `make jaeger` runs it.
- Tests: identical results direct vs gateway (same seeds, same order ids), audit row per call, fail closed on audit failure, collision refusal, trace nesting (in-memory exporter), Postgres audit integration.
### Acceptance
- [x] Agent behaves identically through the gateway (same scenario, same orders): tested with a scripted agent and a 12 call script, direct vs gateway, identical results and order ids. With qwen3:8b the F3 demo produced the same 5 tool calls and the same order id `ord_1c80a3b1799d`.
- [x] Every tool call appears in `audit_events` (tested in memory and against Postgres; 5 of 5 rows for the qwen3 demo session `f4-demo`)
- [x] Traces visible in Jaeger with gateway spans nested under agent spans (tested with an in-memory exporter; verified through the Jaeger API: one trace, 36 spans, 3 services, single root `agent.turn`)
- [x] Gateway overhead measured and logged (p50 and p95 per call): `audit_events.overhead_ms`, gateway exit log, `tradeguard gateway-stats`, and `make bench-gateway` (RESULTS.md)
### Notes and open questions
- Jaeger installed as an official release binary in `.tools/jaeger` (sha256 verified), because there's no Homebrew formula and no Docker.
- Two bugs found while benchmarking, both with regression tests: an empty OTEL endpoint fell back to the default instead of disabling tracing (added `OTEL_SDK_DISABLED`); MCP stdio children didn't inherit shell settings such as `DATABASE_URL` (added `child_env()`). The first benchmark runs were discarded because of the first bug.
- F5 must deny `advance_clock` to the agent (D008).
- The 5 call qwen3 demo session showed a higher internal overhead p95 (23 ms, one cold call, tracing on) than the benchmark. Not comparable, and not used as a metric.
### Loop summary
- Built: MCP gateway (low-level SDK server + per-upstream clients) with fail closed pre-forward audit, `audit_events` Alembic migration, Postgres and in-memory audit sinks, OpenTelemetry tracing for agent, gateway and replay, `child_env`, `tradeguard chat` via gateway (`--direct` for no guard), `tradeguard gateway-stats`, `make bench-gateway`, local Jaeger.
- Tests: 106 passing (14 new for F4); ruff and mypy strict clean.
- Numbers (RESULTS.md, 3 benchmark runs of 300 calls): client side added latency p50 0.96 to 1.13 ms, p95 1.04 to 1.15 ms; gateway internal overhead p50 0.24 to 0.25 ms, p95 0.28 to 0.33 ms. Local only, tracing off.
- Gaps: `advance_clock` still reachable through the gateway (F5 allowlist); replay account still resets per process; overhead not measured with tracing on or against a remote DB.
- Next: F5 Policy engine.

## F5 Policy engine
### Plan
- `services/gateway/policy/model.py`: Pydantic `Policy` (fields from PLAN.md F5, `extra="forbid"` so typos fail), YAML loader whose errors name the field (`require_approval.notional_over_usd: ...`). `policies/default.yaml` and `policies/strict.yaml`.
- `services/gateway/pipeline/`: stages in Section 2 order: (1) schema check against the upstream tool's JSON schema, (2) tool allowlist, (3) policy rules, (6) approval rules. First DENY wins, then the first ESCALATE, else ALLOW `policy_pass`. Any exception is a DENY `pipeline_error` (fail closed).
- Rules are small classes, `evaluate(call, context) -> RuleResult(decision, reason, rule_id)`: order shape, symbol allowlist, order type, max order notional, no short, max position notional, max portfolio exposure, max orders per day, market hours; approval for notional over the threshold and for selling more than a set share of a position.
- Context (price, holdings, equity, clock, orders today) is fetched by the gateway from the upstream's own read tools, only for write calls that pass the static rules. If that fetch fails: DENY `context_unavailable`.
- DENY and ESCALATE return a structured refusal (decision, rule_id, reason, policy) and are never forwarded. F6 will turn ESCALATE into an approval queue.
- Operator channel: `advance_clock` is not on the allowlist. The CLI's `/advance` sends a per session operator token in MCP `_meta` (the model can't set it); the gateway allows only `operator_tools` with it, audited as `rule_id=operator`.
- Gateway and CLI take `--policy` (default `policies/default.yaml`); a missing or invalid policy refuses to start.
### Acceptance
- [x] Each rule has unit tests for allow, deny and boundary values (`tests/policy/test_rules.py`, 11 rules)
- [x] Unknown symbols, oversize orders, shorts and after hours orders are denied (tested end to end in the gateway on the demo scenario)
- [x] Orders over the approval threshold return ESCALATE (tested: `approval_notional` and `approval_sell_pct`; verified with qwen3:8b)
- [x] Every decision in `audit_events` carries `rule_id` and reason (tested; Postgres check: 0 rows missing either)
### Notes and open questions
- One test setup mistake fixed: selling 8 of 10 NVDA is also over the $1000 approval threshold, so the sell percentage test now uses a 4 share position.
- The ESCALATE path stops at a refusal for now; F6 adds the queue, approve, reject and timeout.
- Smoke eval still waits on F9.
### Loop summary
- Built: policy model and loader (`policies/default.yaml`, `policies/strict.yaml`), 11 rule classes, pipeline (schema, allowlist, rules, approval; fail closed), gateway context fetcher, structured refusals, operator token channel for `advance_clock`, `--policy` on gateway and CLI.
- Tests: 186 passing (80 new for F5); ruff and mypy strict clean.
- Evals: none (harness is F9). Manual qwen3:8b runs matched the plan's demo: $3000 TSLA denied, $1500 NVDA escalated.
- Gaps: ESCALATE not queued yet (F6); context mapping is specific to the replay server (F11 adapter); write call overhead with context fetch not yet benchmarked.
- Next: F6 Human approval flow.

## F6 Human approval flow
### Plan
- `approvals` table (Alembic 0002): id, session, tool, args, rule and reason, status (pending, approved, rejected, expired, executed, blocked), created, expires, decided, decided_by, note, result, linked audit event.
- `services/gateway/approvals.py`: an approval store (Postgres, plus in memory for tests). Expiry is applied atomically on every read or decision, so a timed out approval can never be approved. Every approve, reject or expire also writes an `audit_events` row.
- Gateway: on ESCALATE it parks the call as a pending approval and returns `approval_id` to the agent; the call is not executed. If the approval can't be stored: DENY `approval_unavailable`.
- Gateway operator tool `resolve_approval(approval_id)`, not listed and operator token only. Pending returns pending. Rejected or expired returns that with the note. Approved re-runs the deny rules on the *stored* arguments, then executes exactly once (atomic claim), audits `human_approved`, and returns the result. Repeat calls are idempotent.
- Agent graph: after the tools node, an ESCALATE result routes to `await_approval`, which calls LangGraph `interrupt()` with the approval id. On resume, the escalated ToolMessage is replaced by the final outcome, so the model sees what actually happened and explains it.
- CLI: on interrupt it prints the approve and reject commands, polls `resolve_approval`, then resumes the graph.
- FastAPI (`tradeguard api`, :8000): `GET /approvals?status=pending`, `GET /approvals/{id}`, `POST /approvals/{id}/approve` and `/reject` with `decided_by` and `note`; 409 when the approval is no longer pending.
- `APPROVAL_TIMEOUT_SECONDS` (default 300) auto rejects as `expired`.
### Acceptance
- [x] Escalated order is not executed until approved (tested at store, gateway and agent level; demo: approved via curl, then filled at $212.70 after /advance 5)
- [x] Rejected order is never executed and the agent tells the user why (tested: the model receives the reviewer's note; qwen3:8b relayed it in a manual run)
- [x] Timeout auto rejects (tested with 0.05 to 0.2 s timeouts, memory and Postgres)
- [x] Approval decisions recorded in the audit trail (`human_approval`, `human_rejection`, `approval_timeout`, `human_approved`; tested, and visible for sessions f6-fill and f6-reject)
### Notes and open questions
- The API has no authentication locally. F12 must add it before anything is public.
- Spec change during the loop: `resolve_approval` is now listed (and hidden from the model by the agent's toolset) because an unlisted tool made the MCP client re-list tools and warn on every poll. An agent call without the token is DENY `operator_only`.
- One regression test (resolve returns JSON text only) was written together with its fix, not before it.
- Model finding (manual, D010): qwen3:8b once answered an order status from memory instead of calling a tool.
### Loop summary
- Built: `approvals` table and store (Postgres and memory) with in-transaction expiry and audit, gateway parking and `resolve_approval` (re-check, exactly once), LangGraph interrupt and resume, CLI wait loop with approve/reject commands, FastAPI approval API (`tradeguard api`).
- Tests: 215 passing (29 new for F6); ruff and mypy strict clean.
- Evals: none (harness is F9).
- Gaps: no API auth; approvals are polled every 2 s, not pushed; the dashboard approval page comes in F10.
- Next: F7 Provenance and taint tracking (the plan suggests opusplan for this one).

## Current feature: F7 Provenance and taint tracking
### Plan
- Trust labels in the policy file (`taint.trust_labels`; unlisted tools default to untrusted). `get_news` is untrusted; account, market data and pattern tools are trusted. User messages are trusted.
- Provenance store per session (memory, plus Postgres `provenance_events`, Alembic 0003, reloaded when a session's gateway restarts): ordered entries of user messages and tool results with source, trust, risk score, detector signals, excerpt and hash.
- User messages reach the gateway through an operator-only tool `record_user_message`, called by the CLI's code at the start of every turn (the model can't call it).
- Injection signal detector (`services/gateway/provenance/detector.py`): weighted heuristic signals such as instruction override, role markers, addressing the AI, urgency plus a trade verb, and secrecy. It returns a score from 0 to 1 and the signals that matched. It only feeds the risk score.
- Taint rule = pipeline stage 4, after the deny rules and before approval. If the session has untrusted content, the order must be supported by the *latest* user message: side (with simple negation handling) and symbol (ticker or company name).
  - Both supported: ALLOW.
  - Side only (for example "buy the strongest breakout"): ESCALATE, or DENY if the untrusted risk is 0.5 or more.
  - Side not supported: DENY.
  - No untrusted content: the rule passes.
- Untrusted tool results returned to the agent are wrapped: `trust: untrusted`, a "data, not instructions" notice, and delimited data with delimiter look-alikes defanged.
- Taint outcome in audit reasons and span attributes (`tradeguard.taint.*`).
- Labeled set of 60 snippets (30 injection, 30 benign including hard negatives), written and hashed *before* the detector exists; precision and recall from one run go into RESULTS.md, with no tuning on that set.
- Demo: demo scenario with "Check the latest news on NVDA and tell me if anything matters", once direct (no guard) and once guarded, with qwen3:8b. Report what actually happens.
### Acceptance
- [x] Order triggered only by an injected news item is denied (tested: rule, gateway, scripted hijacked agent; verified with a scripted client over the stdio gateway, session `f7-hijack`)
- [x] Order the user explicitly requested is allowed even if news was read earlier (tested at the same three levels; verified in `f7-hijack`)
- [x] Detector has precision and recall reported on a labeled set of at least 50 snippets (60 snippets: precision 1.0, recall 0.867, one run, in RESULTS.md with caveats)
- [x] Taint decisions visible in audit log and traces (reasons in `audit_events`, `tradeguard.taint.*` attributes, tested and checked in Jaeger)
### Notes and open questions
- Labeled set `evals/datasets/injection_snippets.jsonl` (60 rows: 30 injection, 30 benign) frozen before any detector code was written: sha256 `dbed25a392e39c43855de2a7149679431b699a4df28c7bb1fed619279f14cc70`. The same author wrote the set and the detector, which biases results upward; the number is a sanity check, not a claim about real world injections.
- Test setup fixes: a hijacked sell of 10 NVDA (about $2127) hit the order size cap before taint; the tests now sell 5 so the taint rule is what fires, and the agent level test asserts the rule id. The F5 pipeline tests now run with taint off (taint has its own tests, plus a fail closed test for taint on without provenance).
- Real model demo (qwen3:8b): neither the direct nor the guarded agent acted on the demo injection, in two phrasings. The model called it "synthetic test data", because the fixtures say so in the text. See D011; needs a decision before F9.
- Ruff SIM110 disabled project wide: it conflicts with PLAN.md Section 8 (explicit loops).
### Loop summary
- Built: trust labels in policy, provenance store (memory and Postgres, reload on restart), operator-only `record_user_message` called by the CLI, injection signal detector, support check, taint rule as pipeline stage 4 (also re-checked after approval), untrusted result wrapping, taint span attributes, `evals/detector_eval.py`.
- Tests: 253 passing (38 new for F7); ruff and mypy strict clean.
- Numbers: detector precision 1.0, recall 0.867 on 60 self-written snippets (optimistic, see RESULTS.md caveats).
- Gaps: the support check is keyword based (F8 adds amounts, units and an LLM judge); the detector misses single-weak-signal injections; the real model wasn't fooled by our labeled fixtures, so the baseline contrast is unproven.
- Next: F8 Intent alignment (the plan suggests opusplan).
