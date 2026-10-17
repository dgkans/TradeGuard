"""Precision and recall of the injection signal detector on the frozen labeled set.

    uv run python -m evals.detector_eval

Refuses to run if the dataset changed since it was frozen (sha256 below), so a reported
number always refers to the same 60 snippets. Writes a JSON report to evals/reports/.
"""

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.gateway.provenance.detector import THRESHOLD, detect

DATASET = Path("evals/datasets/injection_snippets.jsonl")
FROZEN_SHA256 = "dbed25a392e39c43855de2a7149679431b699a4df28c7bb1fed619279f14cc70"


def git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return result.stdout.strip()


def evaluate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    misses: list[dict[str, Any]] = []
    for row in rows:
        result = detect(row["text"])
        predicted = result.score >= THRESHOLD
        actual = bool(row["injection"])
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
            misses.append(
                {
                    "id": row["id"],
                    "kind": "false_positive",
                    "score": result.score,
                    "signals": result.signals,
                    "text": row["text"],
                }
            )
        elif actual:
            fn += 1
            misses.append(
                {
                    "id": row["id"],
                    "kind": "false_negative",
                    "score": result.score,
                    "signals": result.signals,
                    "text": row["text"],
                }
            )
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "threshold": THRESHOLD,
        "counts": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "misclassified": misses,
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
    report = {
        "date": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "commit": commit,
        "dataset": str(DATASET),
        "dataset_sha256": digest,
        "snippets": len(rows),
        "injection_snippets": sum(1 for row in rows if row["injection"]),
        **evaluate(rows),
    }
    out = Path("evals/reports") / f"detector_{report['date']}.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "misclassified"}, indent=2))
    for miss in report["misclassified"]:
        print(
            f"  {miss['kind']:14} {miss['id']} score={miss['score']} {miss['signals']} "
            f"| {miss['text'][:90]}"
        )
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
