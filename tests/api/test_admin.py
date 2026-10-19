"""Admin login (F10d, F10f): `admin` plus the owner password on the normal login form,
signed tokens, no database."""

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.admin import AdminAuth, AdminLockedError, AdminLoginError
from services.api.app import ApiDeps, create_app
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import MemoryAuditReader, MemoryAuditSink

PASSWORD = "correct horse battery staple"  # noqa: S105 - test value
ADMIN_ONLY = [
    ("get", "/approvals"),
    ("get", "/approvals/apr_x"),
    ("post", "/approvals/apr_x/approve"),
    ("post", "/approvals/apr_x/reject"),
    ("get", "/audit"),
    ("get", "/rules/fired"),
    ("get", "/evals"),
    ("get", "/evals/latest"),
    ("get", "/evals/full_4435fea"),
]


def client(auth: AdminAuth | None = None) -> TestClient:
    audit = MemoryAuditSink()
    deps = ApiDeps(approvals=MemoryApprovalStore(audit=audit), audit=MemoryAuditReader(audit),
                   admin=auth or AdminAuth(PASSWORD))  # fmt: skip
    return TestClient(create_app(deps))


def admin_login(c: TestClient, password: str, username: str = "admin") -> Any:
    return c.post("/auth/login", json={"username": username, "password": password})


def test_admin_logs_in_on_the_normal_form_and_gets_the_admin_endpoints() -> None:
    with client() as c:
        bad = admin_login(c, "nope")
        good = admin_login(c, PASSWORD, username="Admin")  # any case
        token = good.json()["token"]
        me = c.get("/admin/me", headers={"authorization": f"Bearer {token}"})
        audit = c.get("/audit", headers={"authorization": f"Bearer {token}"})
    assert bad.status_code == 401 and bad.json()["detail"] == "wrong username or password"
    assert good.status_code == 200 and good.json()["role"] == "admin"
    assert good.json()["expires_at"]
    assert me.json() == {"admin": True}
    assert audit.status_code == 200


@pytest.mark.parametrize(("method", "path"), ADMIN_ONLY)
def test_operator_endpoints_refuse_without_a_valid_token(method: str, path: str) -> None:
    with client() as c:
        anonymous = getattr(c, method)(path)
        forged = getattr(c, method)(path, headers={"authorization": "Bearer 9999999999.abc"})
    assert anonymous.status_code == 401
    assert forged.status_code == 401


def test_tokens_are_signed_and_expire() -> None:
    auth = AdminAuth(PASSWORD, ttl_s=0.2)
    token = auth.login(PASSWORD).token
    assert auth.verify(token)
    expiry, mac = token.split(".")
    assert not auth.verify(f"{int(expiry) + 3600}.{mac}")  # extending the expiry breaks it
    assert not auth.verify(token[:-1] + ("0" if token[-1] != "0" else "1"))
    time.sleep(0.3)
    assert not auth.verify(token)


def test_tokens_do_not_survive_a_restart() -> None:
    token = AdminAuth(PASSWORD).login(PASSWORD).token
    assert not AdminAuth(PASSWORD).verify(token)  # each process signs with a fresh key


def test_repeated_failures_lock_login_for_a_while() -> None:
    auth = AdminAuth(PASSWORD, max_failures=3, lock_s=60)
    for _ in range(3):
        with pytest.raises(AdminLoginError):
            auth.login("wrong")
    with pytest.raises(AdminLockedError):
        auth.login(PASSWORD)  # even the right password waits out the lock
    with client(auth) as c:
        assert admin_login(c, PASSWORD).status_code == 429


def test_admin_is_off_when_no_password_is_set() -> None:
    with client(AdminAuth(None)) as c:
        login = admin_login(c, "anything")
        audit = c.get("/audit")
    assert login.status_code == 401  # same answer as a wrong password
    assert audit.status_code == 401


def test_trader_pages_stay_open() -> None:
    with client() as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/policies").status_code == 200
        assert c.get("/policies/default").status_code == 200
        assert c.get("/scenarios").status_code == 200
        assert c.get("/market/overview").status_code == 200
        assert c.get("/market/status").json()["live"] is False  # no feed in this app


def test_the_old_admin_login_route_is_gone() -> None:
    with client() as c:
        assert c.post("/admin/login", json={"password": PASSWORD}).status_code in (404, 405)
