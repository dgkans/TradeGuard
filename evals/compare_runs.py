"""Compare two eval runs record by record (the determinism check).

    uv run python -m evals.compare_runs evals/reports/full_X evals/reports/full_X_replay

Two runs match when every (scenario, config, repeat) has the same task outcome, write calls
(tool, args, outcome, rule), tokens, answer and error. Gateway overhead and wall time are
timings, so they are not compared. Exits non zero on any difference.
"""

import json
import sys
from pathlib import Path
from typing import Any

COMPARED = ("task_success", "writes", "agent_tokens", "judge_tokens", "answer", "error")


def load(run_dir: Path) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Records of one run keyed by (scenario, config, repeat)."""
    records = {}
    for line in (run_dir / "records.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        key = (record["scenario_id"], record["config"], record["repeat"])
        records[key] = {field: record[field] for field in COMPARED}
    return records


def differences(
    first: dict[tuple[str, str, int], dict[str, Any]],
    second: dict[tuple[str, str, int], dict[str, Any]],
) -> list[str]:
    """One line per run that is missing from either side or differs in a compared field."""
    out = []
    for key in sorted(set(first) | set(second)):
        if key not in first or key not in second:
            out.append(f"{key}: only in {'second' if key not in first else 'first'} run")
            continue
        changed = []
        for field in COMPARED:
            if first[key][field] != second[key][field]:
                changed.append(field)
        if changed:
            out.append(f"{key}: {', '.join(changed)} differ")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    first, second = load(Path(argv[0])), load(Path(argv[1]))
    diffs = differences(first, second)
    for line in diffs:
        print(line)
    print(f"{len(first)} vs {len(second)} runs, {len(diffs)} differ")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
