"""Files the dashboard reads: policies, eval reports and scenarios.

Every lookup goes by a name from a listing, never by a client supplied path, so a request
cannot reach files outside these folders.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from services.gateway.policy.model import PolicyError, load_policy
from services.replay_server.scenario import ScenarioError, load_scenario


def list_policies(root: Path) -> list[dict[str, str]]:
    """Policy files under `root/policies`, by name (file stem)."""
    out = []
    for path in sorted((root / "policies").glob("*.yaml")):
        out.append({"name": path.stem, "path": f"policies/{path.name}"})
    return out


def policy_path(root: Path, name: str) -> Path | None:
    """Path of the policy called `name`, or None if there is no such policy."""
    for entry in list_policies(root):
        if entry["name"] == name:
            return root / entry["path"]
    return None


def read_policy(root: Path, name: str) -> dict[str, Any] | None:
    """The YAML text and the validated policy as JSON. None if unknown.

    Raises PolicyError if the file exists but is invalid (the gateway would refuse it too).
    """
    path = policy_path(root, name)
    if path is None:
        return None
    policy = load_policy(path)
    return {
        "name": name,
        "path": f"policies/{path.name}",
        "yaml": path.read_text(),
        "policy": policy.model_dump(mode="json"),
    }


def _report_files(root: Path) -> list[Path]:
    return sorted((root / "evals/reports").glob("*/report.json"))


def list_reports(root: Path) -> list[dict[str, Any]]:
    """Eval reports, newest first, with the header fields the dashboard lists."""
    out = []
    for path in _report_files(root):
        try:
            header = json.loads(path.read_text()).get("header", {})
        except (OSError, json.JSONDecodeError):
            continue  # a half written report is skipped, not served
        out.append({
            "name": path.parent.name,
            "date": header.get("date"),
            "commit": header.get("commit"),
            "agent_model": header.get("agent_model"),
            "smoke": bool(header.get("smoke")),
            "mode": header.get("mode"),
            "repeats": header.get("repeats"),
        })  # fmt: skip
    out.sort(key=lambda report: (str(report["date"] or ""), report["name"]), reverse=True)
    return out


def read_report(root: Path, name: str) -> dict[str, Any] | None:
    """The report JSON of the report called `name`, or None if unknown."""
    for path in _report_files(root):
        if path.parent.name == name:
            data: dict[str, Any] = json.loads(path.read_text())
            return data
    return None


def latest_full_report(root: Path) -> tuple[str, dict[str, Any]] | None:
    """Newest full suite report, preferring recorded runs over replays of them."""
    full = []
    for report in list_reports(root):
        if not report["smoke"]:
            full.append(report)
    recorded = []
    for report in full:
        if report["mode"] != "replay":
            recorded.append(report)
    candidates = recorded or full
    if not candidates:
        return None
    name = str(candidates[0]["name"])
    data = read_report(root, name)
    return (name, data) if data is not None else None


@lru_cache(maxsize=4)
def list_scenarios(root: Path) -> tuple[dict[str, Any], ...]:
    """Scenarios offered in the chat: the demo and the tuning suite, never the held out set."""
    out = []
    for path in sorted((root / "scenarios").rglob("*.yaml")):
        try:
            scenario = load_scenario(str(path), root / "scenarios")
        except ScenarioError:
            continue
        if scenario.held_out or "held_out" in path.parts:
            continue
        out.append({
            "id": scenario.id,
            "category": scenario.category,
            "description": scenario.description,
            "watchlist": list(scenario.watchlist),
            "start_time": scenario.start_time.isoformat(),
            "path": str(path.relative_to(root)),
        })  # fmt: skip
    out.sort(key=lambda s: (s["id"] != "demo", s["category"], s["id"]))
    return tuple(out)


def find_scenario(root: Path, scenario_id: str) -> dict[str, Any] | None:
    """The offered scenario with this id, or None (unknown or held out)."""
    for scenario in list_scenarios(root):
        if scenario["id"] == scenario_id:
            return scenario
    return None


__all__ = [
    "PolicyError",
    "find_scenario",
    "latest_full_report",
    "list_policies",
    "list_reports",
    "list_scenarios",
    "policy_path",
    "read_policy",
    "read_report",
]
