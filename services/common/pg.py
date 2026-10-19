"""A Postgres connection that heals after the server drops it (F12).

Neon's free plan suspends the database after a few idle minutes, which closes every open
connection. Each store keeps one connection; before using it after an idle spell, this
checks it with `select 1` and reconnects if it's gone. A failed statement is never retried
(it may have run), so writes stay exactly-once: at worst the first call after an outage
fails, and the next one reconnects.
"""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg


class ResilientConnection:
    """The `execute` and `transaction` the stores use, on a connection kept alive."""

    def __init__(self, url: str, idle_check_s: float = 30.0, **connect: Any) -> None:
        self.url = url
        self.idle_check_s = idle_check_s
        self._connect = connect
        self._conn: psycopg.AsyncConnection[Any] | None = None
        self._last_used = 0.0
        self._in_transaction = False
        self.reconnects = 0

    @classmethod
    async def connect(cls, url: str, **connect: Any) -> "ResilientConnection":
        connection = cls(url, **connect)
        await connection._reconnect()
        return connection

    async def _reconnect(self) -> psycopg.AsyncConnection[Any]:
        if self._conn is not None and not self._conn.closed:
            await self._conn.close()
        self._conn = await psycopg.AsyncConnection.connect(self.url, **self._connect)
        self._last_used = time.monotonic()
        return self._conn

    async def _ready(self) -> psycopg.AsyncConnection[Any]:
        conn = self._conn
        if conn is None or conn.closed or conn.broken:
            self.reconnects += 1
            return await self._reconnect()
        idle = time.monotonic() - self._last_used
        if not self._in_transaction and idle > self.idle_check_s:
            try:
                await conn.execute("select 1")
            except psycopg.OperationalError:
                self.reconnects += 1
                conn = await self._reconnect()
        self._last_used = time.monotonic()
        return conn

    async def execute(self, query: Any, params: Any = None) -> psycopg.AsyncCursor[Any]:
        conn = await self._ready()
        return await conn.execute(query, params)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        conn = await self._ready()
        self._in_transaction = True
        try:
            async with conn.transaction():
                yield
        finally:
            self._in_transaction = False
            self._last_used = time.monotonic()

    @property
    def closed(self) -> bool:
        return self._conn is None or self._conn.closed

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
