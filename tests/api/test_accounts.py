"""Trader accounts (F10e): password hashing, tokens, lockout and the account store."""

import time
import uuid

import pytest

from services.api.accounts import (
    LoginGuard,
    MemoryTraderStore,
    TraderTokens,
    UsernameTakenError,
    hash_password,
    verify_password,
)


def test_passwords_are_salted_scrypt_hashes() -> None:
    first = hash_password("correct horse")
    second = hash_password("correct horse")
    assert first != second  # different salts
    assert first.startswith("scrypt$16384$8$1$")
    assert "correct horse" not in first
    assert verify_password("correct horse", first)
    assert not verify_password("wrong horse", first)
    assert not verify_password("correct horse", "garbage")


def test_tokens_name_the_trader_and_expire() -> None:
    tokens = TraderTokens("s" * 32, ttl_s=0.3)
    token = tokens.issue(42)
    assert tokens.verify(token) == 42
    _trader, expiry, mac = token.split(".")
    assert tokens.verify(f"43.{expiry}.{mac}") is None  # can't switch traders
    assert TraderTokens("t" * 32).verify(token) is None  # another secret
    time.sleep(0.4)
    assert tokens.verify(token) is None


def test_the_same_secret_keeps_tokens_valid_across_restarts() -> None:
    token = TraderTokens("s" * 32).issue(7)
    assert TraderTokens("s" * 32).verify(token) == 7


def test_login_guard_locks_a_username_after_repeated_failures() -> None:
    guard = LoginGuard(max_failures=3, lock_s=60)
    for _ in range(3):
        guard.failed("asha")
    assert guard.locked("asha")
    assert not guard.locked("ben")
    guard.succeeded("ben")
    assert not guard.locked("ben")


async def test_memory_store_creates_finds_and_saves_state() -> None:
    store = MemoryTraderStore()
    asha = await store.create("asha", hash_password("pw123456"), "Asha", "default", 25000)
    with pytest.raises(UsernameTakenError):
        await store.create("ASHA", "x", "Asha", "default", 10000)  # case-insensitive
    found = await store.by_username("Asha")
    assert found is not None and found.id == asha.id and found.starting_cash == 25000
    await store.save_state(asha.id, events=[{"kind": "place"}], transcript=[{"role": "user"}],
                           limits={"max_order_usd": 500})  # fmt: skip
    again = await store.by_id(asha.id)
    assert again is not None
    assert again.events == [{"kind": "place"}] and again.limits == {"max_order_usd": 500}
    assert [t.username for t in await store.list_all()] == ["asha"]
    assert await store.get_clock() is None
    await store.set_clock("2026-10-01T14:35:00+00:00")
    assert await store.get_clock() == "2026-10-01T14:35:00+00:00"


async def test_postgres_store_round_trip() -> None:
    from tests.gateway.test_audit_postgres import database_url, postgres_available

    if not postgres_available():
        pytest.skip("Postgres not reachable")
    from services.api.accounts import PostgresTraderStore
    from services.gateway.audit import migrate

    migrate(database_url())
    name = f"pg_{uuid.uuid4().hex[:8]}"
    async with PostgresTraderStore(database_url()) as store:
        trader = await store.create(name, hash_password("pw123456"), "PG", "strict", 50000)
        with pytest.raises(UsernameTakenError):
            await store.create(name.upper(), "x", "PG", "default", 10000)
        events = [{"kind": "cancel", "order_id": "o1"}]
        transcript = [{"role": "agent", "text": "hi"}]
        await store.save_state(trader.id, events=events, transcript=transcript, limits=None)
    async with PostgresTraderStore(database_url()) as store:  # a "restart"
        back = await store.by_username(name)
        assert back is not None and back.profile == "strict" and back.starting_cash == 50000
        assert back.events == [{"kind": "cancel", "order_id": "o1"}]
        assert back.transcript == [{"role": "agent", "text": "hi"}]
        assert verify_password("pw123456", back.password_hash)
