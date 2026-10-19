"""Connections that survive the database dropping them (F12: Neon suspends idle databases)."""

import psycopg
import pytest

from services.api.accounts import PostgresTraderStore
from services.common.pg import ResilientConnection
from tests.gateway.test_audit_postgres import database_url, postgres_available

pytestmark = pytest.mark.skipif(not postgres_available(), reason="Postgres not reachable")


async def kill(pid: int) -> None:
    """What a suspended Neon compute does to every open connection."""
    async with await psycopg.AsyncConnection.connect(database_url(), autocommit=True) as admin:
        await admin.execute("select pg_terminate_backend(%s)", (pid,))


async def pid_of(conn: ResilientConnection) -> int:
    row = await (await conn.execute("select pg_backend_pid()")).fetchone()
    assert row is not None
    return int(row[0])


async def test_an_idle_connection_reconnects_after_the_server_drops_it() -> None:
    conn = await ResilientConnection.connect(database_url(), idle_check_s=0, autocommit=True)
    try:
        first = await pid_of(conn)
        await kill(first)
        assert await pid_of(conn) != first  # checked, found dead, reconnected
        assert conn.reconnects == 1
        async with conn.transaction():
            await conn.execute("select 1")
    finally:
        await conn.close()


async def test_a_failed_statement_is_not_retried_but_the_next_call_heals() -> None:
    conn = await ResilientConnection.connect(database_url(), idle_check_s=3600, autocommit=True)
    try:
        await kill(await pid_of(conn))
        with pytest.raises(psycopg.OperationalError):
            await conn.execute("select 1")  # not idle long enough to check: fails once
        await conn.execute("select 1")  # the broken connection is replaced
        assert conn.reconnects == 1
    finally:
        await conn.close()


async def test_a_store_keeps_working_after_the_database_drops_its_connection() -> None:
    async with PostgresTraderStore(database_url()) as store:
        store._connection().idle_check_s = 0
        pid = await pid_of(store._connection())
        await kill(pid)
        await store.ping()
        assert await store.by_username("nobody-here") is None
