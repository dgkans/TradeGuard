"""Intent judge agreement with hand labels on the frozen labeled set.

    uv run python -m evals.judge_eval

Runs the LLM judge on every case (not only ambiguous ones) so agreement is measured on the
whole set. Also reports, for reference, what the deterministic stage alone decides and
whether the gateway would have called the judge. Refuses to run if the dataset changed.
Unusable judge output counts as a disagreement.
"""

import asyncio
import hashlib
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.common.config import load_settings
from services.common.llm import make_chat_model
from services.gateway.intent.compare import compare
from services.gateway.intent.judge import LlmJudge
from services.gateway.intent.parser import parse_intent
from services.gateway.pipeline.types import PolicyContext, ToolCall

DATASET = Path("evals/datasets/intent_cases.jsonl")
FROZEN_SHA256 = "eaa1001f2448a02d096f3f079e07dadc93a32bc864aeb56b4218eda447b6030b"


def git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return result.stdout.strip()


def context_of(case: dict[str, Any]) -> PolicyContext:
    ctx = case["context"]
    return PolicyContext(
        price=ctx["price"],
        held_qty=ctx["held_qty"],
        positions_value=ctx["price"] * ctx["held_qty"],
        equity=20000,
        market_open=True,
        orders_today=0,
    )


async def run(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settings = load_settings()
    judge = LlmJudge(
        make_chat_model(settings, json_mode=True, model=settings.judge_model),
        model=f"{settings.llm_provider}:{settings.judge_model}",
    )
    results = []
    for case in rows:
        call = ToolCall(case["call"]["tool"], case["call"]["args"])
        context = context_of(case)
        parsed = parse_intent(case["user_text"])
        deterministic = compare(parsed, call, context, tolerance_pct=10)
        started = time.perf_counter()
        try:
            verdict = await judge.judge(case["user_text"], call, context)
            judged: bool | None = verdict.aligned
            confidence: float | None = verdict.confidence
            reason, error = verdict.reason, None
        except Exception as exc:
            judged, confidence, reason, error = None, None, "", f"{type(exc).__name__}: {exc}"
        results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "label": case["aligned"],
                "judge_aligned": judged,
                "judge_confidence": confidence,
                "judge_reason": reason,
                "error": error,
                "agree": judged == case["aligned"],
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "gateway_would_call_judge": deterministic.needs_judge,
                "deterministic": f"{deterministic.decision} {deterministic.rule_id}",
            }
        )
        mark = "OK " if judged == case["aligned"] else "XX "
        print(
            f"{case['id']} {case['category']:16} label={case['aligned']!s:5} "
            f"judge={judged!s:5} conf={confidence} {mark}| {case['user_text'][:50]}",
            flush=True,
        )
    return {"model": judge.model, "prompt_version": judge.prompt_version, "results": results}


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, list[bool]] = defaultdict(list)
    for row in results:
        by_category[row["category"]].append(row["agree"])
    ambiguous = [row for row in results if row["gateway_would_call_judge"]]
    latencies = sorted(row["latency_ms"] for row in results)
    return {
        "cases": len(results),
        "agreement": round(sum(r["agree"] for r in results) / len(results), 4),
        "agreed": sum(r["agree"] for r in results),
        "judge_errors": sum(1 for r in results if r["error"]),
        "agreement_on_cases_gateway_sends_to_judge": {
            "cases": len(ambiguous),
            "agreed": sum(r["agree"] for r in ambiguous),
        },
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_category.items())},
        "latency_ms_p50": latencies[len(latencies) // 2],
        "latency_ms_max": latencies[-1],
    }


def main() -> int:
    raw = DATASET.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != FROZEN_SHA256:
        print(f"dataset changed: sha256 {digest} != frozen {FROZEN_SHA256}", file=sys.stderr)
        return 2
    rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    commit = git(["rev-parse", "--short", "HEAD"])
    if git(["status", "--porcelain", "--untracked-files=no"]):
        commit += "-dirty"
    outcome = asyncio.run(run(rows))
    report = {
        "date": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "commit": commit,
        "dataset": str(DATASET),
        "dataset_sha256": digest,
        "model": outcome["model"],
        "prompt_version": outcome["prompt_version"],
        "temperature": 0,
        **summarize(outcome["results"]),
        "results": outcome["results"],
    }
    out = Path("evals/reports") / f"judge_{report['date']}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
