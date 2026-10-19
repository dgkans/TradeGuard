"""Dashboard API read endpoints: audit log, policies, eval reports, scenarios."""

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from services.api.admin import AdminAuth
from services.api.app import ApiDeps, create_app
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import (
    AuditOutcome,
    DecisionRecord,
    MemoryAuditReader,
    MemoryAuditSink,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


async def seeded_audit() -> MemoryAuditSink:
    sink = MemoryAuditSink()
    rows: list[tuple[str, str, str, str]] = [
        ("s1", "get_quote", "ALLOW", "pass"),
        ("s1", "place_order", "DENY", "max_order_notional"),
        ("s2", "place_order", "ESCALATE", "approval_notional"),
        ("s2", "place_order", "DENY", "max_order_notional"),
    ]
    for session, tool, decision, rule in rows:
        event_id = await sink.record_decision(
            DecisionRecord(
                session_id=session, upstream="replay", tool=tool, args={"symbol": "NVDA"},
                decision=decision, rule_id=rule, reason=f"{rule} reason",  # type: ignore[arg-type]
                trace_id="ab" * 16, span_id="cd" * 8,
            )
        )  # fmt: skip
        await sink.record_outcome(event_id, AuditOutcome(2.0, 1.5, False, "ok"))
    return sink


def client_for(sink: MemoryAuditSink | None = None, root: Path = REPO_ROOT) -> TestClient:
    """A client logged in as admin (these endpoints are the owner's admin area, F10d)."""
    audit = sink or MemoryAuditSink()
    admin = AdminAuth("test-admin")
    deps = ApiDeps(
        approvals=MemoryApprovalStore(audit=audit),
        audit=MemoryAuditReader(audit),
        repo_root=root,
        jaeger_ui_url="http://jaeger.local:16686",
        admin=admin,
    )
    token = admin.login("test-admin").token
    return TestClient(create_app(deps), headers={"authorization": f"Bearer {token}"})


async def test_audit_lists_newest_first_with_trace_links() -> None:
    with client_for(await seeded_audit()) as client:
        events = client.get("/audit").json()["events"]
    assert [e["id"] for e in events] == [4, 3, 2, 1]
    first = events[0]
    assert first["decision"] == "DENY"
    assert first["rule_id"] == "max_order_notional"
    assert first["overhead_ms"] == 0.5
    assert first["created_at"]
    assert first["trace_url"] == "http://jaeger.local:16686/trace/" + "ab" * 16


async def test_audit_filters_and_pages() -> None:
    with client_for(await seeded_audit()) as client:
        by_session = client.get("/audit", params={"session_id": "s2"}).json()["events"]
        denied = client.get("/audit", params={"decision": "DENY"}).json()["events"]
        page = client.get("/audit", params={"limit": 2}).json()
        older = client.get("/audit", params={"limit": 2, "before_id": page["next_before_id"]})
        bad = client.get("/audit", params={"limit": 0})
    assert [e["id"] for e in by_session] == [4, 3]
    assert [e["id"] for e in denied] == [4, 2]
    assert [e["id"] for e in page["events"]] == [4, 3]
    assert [e["id"] for e in older.json()["events"]] == [2, 1]
    assert older.json()["next_before_id"] is None
    assert bad.status_code == 422


async def test_rules_fired_counts_only_blocking_decisions() -> None:
    with client_for(await seeded_audit()) as client:
        counts = client.get("/rules/fired").json()["rules"]
    assert counts == [
        {"rule_id": "max_order_notional", "decision": "DENY", "count": 2,
         "title": "Order is over your per-order limit"},
        {"rule_id": "approval_notional", "decision": "ESCALATE", "count": 1,
         "title": "Large order: needs your approval"},
    ]  # fmt: skip


def test_policies_list_and_show_yaml() -> None:
    with client_for() as client:
        names = [p["name"] for p in client.get("/policies").json()["policies"]]
        default = client.get("/policies/default").json()
        missing = client.get("/policies/nope")
        sneaky = client.get("/policies/..%2F.env")
    assert names == ["default", "rules_only", "strict"]
    assert default["path"] == "policies/default.yaml"
    assert "max_order_notional" in default["yaml"]
    assert default["policy"]["taint"]["enabled"] is True
    assert missing.status_code == 404
    assert sneaky.status_code == 404


def write_report(root: Path, name: str, header: dict[str, Any]) -> None:
    folder = root / "evals/reports" / name
    folder.mkdir(parents=True)
    payload = {"header": header, "tuning": {}, "held_out": {}, "by_category": {}}
    (folder / "report.json").write_text(json.dumps(payload))


def test_eval_reports_latest_full_suite_and_lookup(tmp_path: Path) -> None:
    (tmp_path / "policies").mkdir()
    write_report(tmp_path, "full_a", {"date": "2026-10-01 10:00 UTC", "smoke": False})
    write_report(tmp_path, "full_b", {"date": "2026-10-05 10:00 UTC", "smoke": False})
    write_report(tmp_path, "smoke_x", {"date": "2026-10-09 10:00 UTC", "smoke": True})
    with client_for(root=tmp_path) as client:
        reports = client.get("/evals").json()["reports"]
        latest = client.get("/evals/latest").json()
        one = client.get("/evals/full_a").json()
        missing = client.get("/evals/nope")
        sneaky = client.get("/evals/..%2F..%2Fpolicies")
    assert [r["name"] for r in reports] == ["smoke_x", "full_b", "full_a"]
    assert reports[0]["smoke"] is True
    assert latest["name"] == "full_b"
    assert latest["report"]["header"]["date"] == "2026-10-05 10:00 UTC"
    assert one["report"]["header"]["date"] == "2026-10-01 10:00 UTC"
    assert missing.status_code == 404
    assert sneaky.status_code == 404


def test_latest_eval_prefers_a_recorded_run_over_its_replay(tmp_path: Path) -> None:
    # A replay reproduces outcomes but not timings (overhead reads as about 0 ms).
    write_report(tmp_path, "full_a", {"date": "2026-10-01 10:00 UTC", "smoke": False,
                                      "mode": "record"})  # fmt: skip
    write_report(tmp_path, "full_a_replay", {"date": "2026-10-02 10:00 UTC", "smoke": False,
                                             "mode": "replay"})  # fmt: skip
    with client_for(root=tmp_path) as client:
        assert client.get("/evals/latest").json()["name"] == "full_a"


def test_latest_eval_ignores_other_suites(tmp_path: Path) -> None:
    write_report(tmp_path, "full_a", {"date": "2026-10-01 10:00 UTC", "smoke": False})
    write_report(tmp_path, "hard_b", {"date": "2026-10-03 10:00 UTC", "smoke": False,
                                      "suite": "scenarios_hard"})  # fmt: skip
    with client_for(root=tmp_path) as client:
        assert client.get("/evals/latest").json()["name"] == "full_a"
        listed = {r["name"]: r["suite"] for r in client.get("/evals").json()["reports"]}
    assert listed == {"full_a": "scenarios", "hard_b": "scenarios_hard"}


def test_latest_eval_is_the_llm_run_not_the_scripted_worst_case(tmp_path: Path) -> None:
    """Regression: the worst case agent's report is newer and was served as "latest"."""
    write_report(tmp_path, "full_a", {"date": "2026-10-01 10:00 UTC", "smoke": False,
                                      "mode": "record"})  # fmt: skip
    write_report(tmp_path, "attacker_b", {"date": "2026-10-05 10:00 UTC", "smoke": False,
                                          "mode": "record", "agent": "attacker"})  # fmt: skip
    with client_for(root=tmp_path) as client:
        assert client.get("/evals/latest").json()["name"] == "full_a"
        worst = client.get("/evals/latest", params={"agent": "attacker"}).json()
        bad = client.get("/evals/latest", params={"agent": "robot"})
    assert worst["name"] == "attacker_b"
    assert bad.status_code == 422


def test_real_repo_latest_eval_is_an_llm_run() -> None:
    with client_for() as client:
        latest = client.get("/evals/latest").json()
    assert latest["report"]["header"].get("agent") is None
    assert "qwen" in latest["report"]["header"]["agent_model"]


def test_latest_eval_is_404_when_there_is_none(tmp_path: Path) -> None:
    with client_for(root=tmp_path) as client:
        assert client.get("/evals/latest").status_code == 404
        assert client.get("/evals").json() == {"reports": []}


def test_real_repo_latest_report_is_the_full_suite_run() -> None:
    with client_for() as client:
        latest = client.get("/evals/latest").json()
    assert latest["report"]["header"]["smoke"] is False
    assert set(latest["report"]["tuning"]) == {"no_guard", "rules_only", "full_guard"}


def test_scenarios_exclude_the_held_out_set() -> None:
    with client_for() as client:
        scenarios = client.get("/scenarios").json()["scenarios"]
    ids = {s["id"] for s in scenarios}
    assert "demo" in ids
    assert "inj_001" in ids
    assert len(ids) == 82  # 80 tuning scenarios plus the demo and the trading desk
    assert "desk" in ids
    assert all(not s["id"].startswith("held") for s in scenarios)
    demo = next(s for s in scenarios if s["id"] == "demo")
    assert demo["category"] == "demo"


async def test_rules_fired_can_be_limited_to_trader_sessions() -> None:
    sink = await seeded_audit()  # sessions s1 and s2
    await sink.record_decision(
        DecisionRecord(session_id="web-asha-1", upstream="replay", tool="place_order",
                       args={}, decision="DENY", rule_id="market_hours", reason="closed")
    )  # fmt: skip
    with client_for(sink) as client:
        traders = client.get("/rules/fired", params={"scope": "traders"}).json()["rules"]
        everything = client.get("/rules/fired").json()["rules"]
    assert [r["rule_id"] for r in traders] == ["market_hours"]
    assert len(everything) == 3
