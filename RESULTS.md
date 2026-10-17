# Results

No eval runs yet (the harness is F9). Every number here must come from an actual run (PLAN.md Section 3, Loop rule 2).

## Operational: gateway overhead benchmark (F4) | 2026-10-17 UTC | code of the commit "F4: Gateway proxy (pass through)", measured on its working tree just before committing (reports say c9f1a8b-dirty)
Source: `make bench-gateway`, JSON reports in `evals/reports/gateway_overhead_*.json`.
Setup: Apple M5 MacBook, 16 GB. Replay server over stdio, demo scenario. 300 timed calls per path after 20 warmup calls, cycling get_quote, get_bars (50 x 5m), get_account, scan_watchlist (3 symbols). Gateway over stdio with the Postgres audit sink (local Homebrew PostgreSQL 16). Tracing disabled on both paths. No LLM involved.

| Run (UTC) | Direct p50 / p95 ms | Through gateway p50 / p95 ms | Added latency p50 / p95 ms | Gateway internal overhead p50 / p95 ms |
|---|---|---|---|---|
| 20261017T020946Z | 0.609 / 2.071 | 1.741 / 3.197 | 1.132 / 1.126 | 0.244 / 0.277 |
| 20261017T020949Z | 0.764 / 2.212 | 1.727 / 3.253 | 0.963 / 1.041 | 0.246 / 0.309 |
| 20261017T020952Z | 0.752 / 2.098 | 1.846 / 3.248 | 1.094 / 1.15 | 0.245 / 0.332 |

"Added latency" is the gateway path's percentile minus the direct path's percentile, as seen by the client. It includes the extra stdio hop and serialization. "Internal overhead" is the gateway's own time per call minus the time waiting on the upstream, from `audit_events.overhead_ms`; it includes the pre-forward audit insert.
Not yet measured: overhead with tracing on, over HTTP, or against a remote database (Neon, F12).


<!-- Template for each run:

## Run <date> | commit <hash>
Agent model:            Judge model:            Temperature:
Scenarios: <n> (held out: <n>)   Repeats: <n>

| Metric | no_guard | rules_only | full_guard |
|---|---|---|---|
| Unsafe action block rate | | | |
| False block rate | | | |
| Task success rate | | | |
| Injection success rate | | | |
| Escalation rate | | | |
| p95 gateway overhead (ms) | | | |
| Tokens per scenario | | | |

Held out set:
Notes:
-->
