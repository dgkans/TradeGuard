"""Write eval reports (Markdown and JSON) for a set of run records."""

import json
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.metrics import RATE_KEYS, aggregate, by_category
from evals.records import RunRecord
from services.common.config import load_settings

LABELS = {
    "unsafe_action_block_rate": "Unsafe action block rate",
    "false_block_rate": "False block rate",
    "task_success_rate": "Task success rate",
    "injection_success_rate": "Injection success rate",
    "escalation_rate": "Escalation rate",
    "gateway_overhead_p50_ms": "p50 gateway overhead (ms)",
    "gateway_overhead_p95_ms": "p95 gateway overhead (ms)",
    "agent_tokens_per_scenario": "Agent tokens per scenario",
    "judge_tokens_per_scenario": "Judge tokens per scenario",
}
CONFIG_ORDER = ["no_guard", "rules_only", "full_guard"]
PERCENT_KEYS = {
    "unsafe_action_block_rate",
    "false_block_rate",
    "task_success_rate",
    "injection_success_rate",
    "escalation_rate",
}


def git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return result.stdout.strip()


def commit_label() -> str:
    commit = git(["rev-parse", "--short", "HEAD"])
    if git(["status", "--porcelain", "--untracked-files=no"]):
        commit += "-dirty"
    return commit


def ollama_version() -> str:
    try:
        result = subprocess.run(
            ["ollama", "--version"],  # noqa: S607 - local dev tool on PATH
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip().split()[-1] if result.stdout.strip() else "unknown"


def fmt(key: str, cell: dict[str, Any]) -> str:
    if cell["mean"] is None:
        return "n/a"

    def one(value: float) -> str:
        if key in PERCENT_KEYS:
            return f"{value * 100:.1f}%"
        return f"{value:.1f}" if value < 1000 else f"{value:,.0f}"

    if cell["min"] == cell["max"]:
        return f"{one(cell['mean'])} (all {cell['repeats']})"
    return f"{one(cell['mean'])} [{one(cell['min'])}, {one(cell['max'])}]"


def table(records: list[RunRecord]) -> tuple[str, dict[str, Any]]:
    data = aggregate(records)
    configs = [c for c in CONFIG_ORDER if c in data] + [c for c in data if c not in CONFIG_ORDER]
    lines = ["| Metric | " + " | ".join(configs) + " |", "|---|" + "---|" * len(configs)]
    for key in RATE_KEYS:
        cells = [fmt(key, data[c][key]) for c in configs]
        lines.append(f"| {LABELS[key]} | " + " | ".join(cells) + " |")
    counts = []
    for c in configs:
        d = data[c]
        counts.append(
            f"{c}: {d['scenarios']['mean']:.0f} scenarios, "
            f"{d['write_calls']['mean']:.1f} write calls per repeat "
            f"({d['unsafe_attempted']['mean']:.1f} unsafe, "
            f"{d['safe_attempted']['mean']:.1f} safe), "
            f"{d['unsafe_executed']['mean']:.1f} unsafe executed, "
            f"{d['run_errors']['mean']:.1f} run errors"
        )
    return "\n".join(lines) + "\n\nCounts (mean per repeat): " + "; ".join(counts) + "\n", data


def latest(pattern: str) -> dict[str, Any] | None:
    reports = sorted(Path("evals/reports").glob(pattern))
    return json.loads(reports[-1].read_text()) if reports else None


def write_report(records: list[RunRecord], out: Path, meta: dict[str, Any]) -> Path:
    settings = load_settings()
    tuning = [r for r in records if not r.held_out]
    held = [r for r in records if r.held_out]
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    header = {
        "date": stamp,
        "commit": commit_label(),
        "agent_model": f"{settings.llm_provider}:{settings.llm_model}",
        "judge_model": f"{settings.llm_provider}:{settings.judge_model}",
        "temperature": 0,
        "ollama": ollama_version(),
        **meta,
    }
    tuning_md, tuning_data = table(tuning) if tuning else ("(none)\n", {})
    held_md, held_data = table(held) if held else ("(none in this run)\n", {})
    categories = by_category(records)
    failures: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        if not record.task_success:
            failures[record.config][record.scenario_id].append(
                f"r{record.repeat}: {'; '.join(record.failures)[:200]}"
            )
    judge = latest("judge_*.json")
    detector = latest("detector_*.json")

    md = [f"# TradeGuard eval report ({'smoke' if meta.get('smoke') else 'full suite'})", ""]
    md.append(" | ".join(f"{k}: {v}" for k, v in header.items()))
    md += [
        "",
        "Mean over repeats, with [min, max] when repeats differ.",
        "",
        "## Tuning set (not held out)",
        "",
        tuning_md,
        "## Held out set",
        "",
        held_md,
        "## Task success by category",
        "",
    ]
    cats = sorted({c for row in categories.values() for c in row})
    md.append("| Config | " + " | ".join(cats) + " |")
    md.append("|---|" + "---|" * len(cats))
    for config in [c for c in CONFIG_ORDER if c in categories]:
        cells = []
        for cat in cats:
            value = categories[config].get(cat)
            cells.append("n/a" if value is None else f"{value * 100:.0f}%")
        md.append(f"| {config} | " + " | ".join(cells) + " |")
    md += ["", "## Component checks (separate runs)", ""]
    if judge:
        amb = judge["agreement_on_cases_gateway_sends_to_judge"]
        md.append(
            f"- Judge agreement: {judge['agreed']}/{judge['cases']} overall, "
            f"{amb['agreed']}/{amb['cases']} on ambiguous cases ({judge['model']}, "
            f"{judge['prompt_version']}, {judge['date']})"
        )
    if detector:
        md.append(
            f"- Injection detector: precision {detector['precision']}, recall "
            f"{detector['recall']} on {detector['snippets']} snippets ({detector['date']})"
        )
    md += ["", "## Failed runs", ""]
    for config in [c for c in CONFIG_ORDER if c in failures]:
        md.append(f"### {config} ({len(failures[config])} scenarios)")
        for scenario_id, reasons in sorted(failures[config].items()):
            md.append(f"- {scenario_id}: " + " | ".join(reasons))
        md.append("")

    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text("\n".join(md) + "\n")
    payload = {
        "header": header,
        "tuning": tuning_data,
        "held_out": held_data,
        "by_category": categories,
        "judge": judge and {k: judge[k] for k in judge if k != "results"},
        "detector": detector and {k: detector[k] for k in detector if k != "misclassified"},
    }
    (out / "report.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return out / "report.md"
