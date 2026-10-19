"""Admin login (F10d): one owner password from settings, signed tokens, no database."""

import time

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


def test_login_returns_a_token_that_opens_admin_endpoints() -> None:
    with client() as c:
        bad = c.post("/admin/login", json={"password": "nope"})
        good = c.post("/admin/login", json={"password": PASSWORD})
        token = good.json()["token"]
        me = c.get("/admin/me", headers={"authorization": f"Bearer {token}"})
        audit = c.get("/audit", headers={"authorization": f"Bearer {token}"})
    assert bad.status_code == 401
    assert good.status_code == 200 and good.json()["expires_at"]
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
        assert c.post("/admin/login", json={"password": PASSWORD}).status_code == 429


def test_admin_is_off_when_no_password_is_set() -> None:
    with client(AdminAuth(None)) as c:
        login = c.post("/admin/login", json={"password": ""})
        audit = c.get("/audit")
    assert login.status_code == 503
    assert audit.status_code == 401


def test_trader_pages_stay_open() -> None:
    with client() as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/policies").status_code == 200
        assert c.get("/policies/default").status_code == 200
        assert c.get("/scenarios").status_code == 200
        assert c.get("/market/overview").status_code == 200
