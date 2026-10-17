"""The determinism check: record by record comparison of two runs."""

import json
from pathlib import Path
from typing import Any

from evals.compare_runs import differences, load, main


def record(scenario: str, success: bool = True, overhead: float = 0.2) -> dict[str, Any]:
    return {
        "scenario_id": scenario,
        "config": "full_guard",
        "repeat": 1,
        "task_success": success,
        "writes": [],
        "agent_tokens": 10,
        "judge_tokens": 0,
        "answer": "ok",
        "error": None,
        "overheads_ms": [overhead],
        "seconds": overhead,
    }


def write_run(folder: Path, records: list[dict[str, Any]]) -> Path:
    folder.mkdir()
    lines = [json.dumps(r) for r in records]
    (folder / "records.jsonl").write_text("\n".join(lines) + "\n")
    return folder


def test_timings_are_ignored_and_outcomes_are_compared(tmp_path: Path) -> None:
    a = write_run(tmp_path / "a", [record("x"), record("y")])
    b = write_run(tmp_path / "b", [record("x", overhead=9.0), record("y", success=False)])
    diffs = differences(load(a), load(b))
    assert diffs == ["('y', 'full_guard', 1): task_success differ"]
    assert main([str(a), str(b)]) == 1
    assert main([str(a), str(a)]) == 0


def test_a_run_missing_on_one_side_is_a_difference(tmp_path: Path) -> None:
    a = write_run(tmp_path / "a", [record("x"), record("y")])
    b = write_run(tmp_path / "b", [record("x")])
    assert differences(load(a), load(b)) == ["('y', 'full_guard', 1): only in first run"]
