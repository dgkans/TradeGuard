"""Production settings and the public API's guard rails (F12)."""

import pytest
from fastapi.testclient import TestClient

from services.api.accounts import MemoryTraderStore, TraderTokens
from services.api.admin import AdminAuth
from services.api.app import Accounts, ApiDeps, IpLimiter, create_app
from services.common.config import ConfigError, load_settings
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import DecisionRecord, MemoryAuditReader, MemoryAuditSink

DASHBOARD = "https://tradeguard.pages.dev"


def app_with(**extra: object) -> tuple[TestClient, MemoryAuditSink]:
    audit = MemoryAuditSink()
    deps = ApiDeps(approvals=MemoryApprovalStore(audit=audit), audit=MemoryAuditReader(audit),
                   admin=AdminAuth("owner-pass"),
                   accounts=Accounts(MemoryTraderStore(), TraderTokens("k" * 32),
                                     signups=IpLimiter(2)),
                   **extra)  # type: ignore[arg-type]  # fmt: skip
    return TestClient(create_app(deps)), audit


def test_production_refuses_to_start_half_configured() -> None:
    with pytest.raises(ConfigError, match=r"ADMIN_PASSWORD.*TRADER_TOKEN_SECRET.*LLM_PROVIDER"):
        load_settings(env_file=None, environ={"TRADEGUARD_ENV": "production"})
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        load_settings(env_file=None, environ={
            "TRADEGUARD_ENV": "production", "ADMIN_PASSWORD": "x", "TRADER_TOKEN_SECRET": "y",
            "LLM_PROVIDER": "groq"})  # fmt: skip
    ok = load_settings(env_file=None, environ={
        "TRADEGUARD_ENV": "production", "ADMIN_PASSWORD": "x", "TRADER_TOKEN_SECRET": "y",
        "LLM_PROVIDER": "groq", "GROQ_API_KEY": "gsk_x"})  # fmt: skip
    assert ok.tradeguard_env == "production"


def test_cors_allows_only_the_dashboard() -> None:
    client, _ = app_with(cors_origins=[DASHBOARD])
    with client:
        preflight = {"origin": DASHBOARD, "access-control-request-method": "GET",
                     "access-control-request-headers": "x-trader-token"}  # fmt: skip
        allowed = client.options("/me", headers=preflight)
        other = client.get("/healthz", headers={"origin": "https://evil.example"})
    assert allowed.headers["access-control-allow-origin"] == DASHBOARD
    assert "x-trader-token" in allowed.headers["access-control-allow-headers"]
    assert "access-control-allow-origin" not in other.headers


def test_signups_are_limited_per_address_behind_the_proxy() -> None:
    client, _ = app_with(trust_proxy=True)
    with client:

        def signup(name: str, ip: str) -> int:
            body = {"username": name, "password": "long-enough-1"}
            response = client.post("/auth/signup", json=body,
                                   headers={"x-forwarded-for": f"{ip}, 10.0.0.1"})  # fmt: skip
            return response.status_code

        codes = [signup(f"user{i}", "203.0.113.7") for i in range(3)]
        elsewhere = signup("user9", "198.51.100.2")
    assert codes == [201, 201, 429]
    assert elsewhere == 201


def test_readyz_reports_the_database_and_prices() -> None:
    client, _ = app_with()
    with client:
        ready = client.get("/readyz").json()
    assert ready == {"ready": True, "database": "ok", "prices": "off"}


def test_no_trace_links_without_a_trace_viewer() -> None:
    client, audit = app_with(jaeger_ui_url="")
    with client:
        login = {"username": "admin", "password": "owner-pass"}
        token = client.post("/auth/login", json=login).json()["token"]
        import asyncio

        asyncio.run(audit.record_decision(DecisionRecord(
            session_id="s", upstream="replay", tool="get_quote", args={}, decision="ALLOW",
            rule_id="allowlist", reason="ok", trace_id="ab" * 16)))  # fmt: skip
        events = client.get("/audit", headers={"authorization": f"Bearer {token}"}).json()
    assert events["events"][0]["trace_url"] is None
