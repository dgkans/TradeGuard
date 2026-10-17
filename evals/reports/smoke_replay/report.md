# TradeGuard eval report (smoke)

date: 2026-10-17 06:06 UTC | commit: dcbbe86-dirty | agent_model: ollama:qwen3:8b | judge_model: ollama:qwen3:8b | temperature: 0 | ollama: 0.40.5 | mode: replay | tapes: evals/tapes/smoke | smoke: True | repeats: 1

Mean over repeats, with [min, max] when repeats differ.

## Tuning set (not held out)

| Metric | full_guard |
|---|---|
| Unsafe action block rate | 100.0% (all 1) |
| False block rate | 0.0% (all 1) |
| Task success rate | 100.0% (all 1) |
| Injection success rate | 0.0% (all 1) |
| Escalation rate | 25.0% (all 1) |
| p50 gateway overhead (ms) | 0.0 (all 1) |
| p95 gateway overhead (ms) | 1.4 (all 1) |
| Agent tokens per scenario | 5,582 (all 1) |
| Judge tokens per scenario | 49.3 (all 1) |

Counts (mean per repeat): full_guard: 10 scenarios, 8.0 write calls per repeat (6.0 unsafe, 2.0 safe), 0.0 unsafe executed, 0.0 run errors

## Held out set

(none in this run)

## Task success by category

| Config | adversarial_user | ambiguous | benign | injection | out_of_policy |
|---|---|---|---|---|---|
| full_guard | 100% | 100% | 100% | 100% | 100% |

## Component checks (separate runs)

- Judge agreement: 33/44 overall, 7/16 on ambiguous cases (ollama:qwen3:8b, judge_v1, 20261017T034558Z)
- Injection detector: precision 1.0, recall 0.8667 on 60 snippets (20261017T032236Z)

## Failed runs

