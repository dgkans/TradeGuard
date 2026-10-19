"""Trader accounts (F10e): passwords, login tokens, lockout, and the account store.

Passwords are hashed with scrypt from the standard library (n=2^14, r=8, p=1, 16 byte
salt) and checked in constant time. Login tokens are `<trader id>.<expiry ms>.<HMAC>`,
signed with TRADER_TOKEN_SECRET, so they survive restarts but can't be forged or moved to
another trader. A username is locked for a while after repeated wrong passwords.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


class UsernameTakenError(Exception):
    """Someone already has this username."""


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    encoded = (base64.b64encode(salt).decode(), base64.b64encode(digest).decode())
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${encoded[0]}${encoded[1]}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


class TraderTokens:
    def __init__(self, secret: str, ttl_s: float = 7 * 24 * 3600) -> None:
        self._key = hashlib.sha256(secret.encode()).digest()
        self.ttl_s = ttl_s

    def _sign(self, trader_id: int, expiry_ms: int) -> str:
        message = f"trader:{trader_id}:{expiry_ms}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def issue(self, trader_id: int) -> str:
        expiry_ms = int((time.time() + self.ttl_s) * 1000)
        return f"{trader_id}.{expiry_ms}.{self._sign(trader_id, expiry_ms)}"

    def verify(self, token: str | None) -> int | None:
        """The trader id for a valid, unexpired token; None otherwise."""
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        trader_id, expiry_ms = int(parts[0]), int(parts[1])
        if not hmac.compare_digest(parts[2], self._sign(trader_id, expiry_ms)):
            return None
        if time.time() * 1000 >= expiry_ms:
            return None
        return trader_id


class LoginGuard:
    """Locks a username for `lock_s` after `max_failures` wrong passwords in a row."""

    def __init__(self, max_failures: int = 5, lock_s: float = 300) -> None:
        self.max_failures = max_failures
        self.lock_s = lock_s
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def locked(self, username: str) -> bool:
        return time.monotonic() < self._locked_until.get(username.lower(), 0.0)

    def failed(self, username: str) -> None:
        key = username.lower()
        self._failures[key] = self._failures.get(key, 0) + 1
        if self._failures[key] >= self.max_failures:
            self._failures[key] = 0
            self._locked_until[key] = time.monotonic() + self.lock_s

    def succeeded(self, username: str) -> None:
        self._failures.pop(username.lower(), None)


@dataclass
class Trader:
    id: int
    username: str
    display_name: str
    password_hash: str
    profile: str
    starting_cash: float
    limits: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "username": self.username, "display_name": self.display_name,
                "profile": self.profile, "starting_cash": self.starting_cash}  # fmt: skip


class TraderStore(Protocol):
    async def create(self, username: str, password_hash: str, display_name: str,
                     profile: str, starting_cash: float) -> Trader: ...  # fmt: skip

    async def by_username(self, username: str) -> Trader | None: ...

    async def by_id(self, trader_id: int) -> Trader | None: ...

    async def list_all(self) -> list[Trader]: ...

    async def save_state(self, trader_id: int, *, events: list[dict[str, Any]],
                         transcript: list[dict[str, Any]],
                         limits: dict[str, Any] | None) -> None: ...  # fmt: skip

    async def get_clock(self) -> str | None: ...

    async def set_clock(self, iso: str) -> None: ...


class MemoryTraderStore:
    """For tests and runs without a database."""

    def __init__(self) -> None:
        self._traders: dict[int, Trader] = {}
        self._clock: str | None = None

    async def create(self, username: str, password_hash: str, display_name: str,
                     profile: str, starting_cash: float) -> Trader:  # fmt: skip
        key = username.lower()
        for trader in self._traders.values():
            if trader.username == key:
                raise UsernameTakenError(f"{username} is taken")
        trader = Trader(len(self._traders) + 1, key, display_name, password_hash, profile,
                        float(starting_cash))  # fmt: skip
        self._traders[trader.id] = trader
        return trader

    async def by_username(self, username: str) -> Trader | None:
        for trader in self._traders.values():
            if trader.username == username.lower():
                return trader
        return None

    async def by_id(self, trader_id: int) -> Trader | None:
        return self._traders.get(trader_id)

    async def list_all(self) -> list[Trader]:
        return sorted(self._traders.values(), key=lambda trader: trader.id)

    async def save_state(self, trader_id: int, *, events: list[dict[str, Any]],
                         transcript: list[dict[str, Any]],
                         limits: dict[str, Any] | None) -> None:  # fmt: skip
        trader = self._traders[trader_id]
        trader.events = json.loads(json.dumps(events, default=str))
        trader.transcript = json.loads(json.dumps(transcript, default=str))
        trader.limits = limits

    async def get_clock(self) -> str | None:
        return self._clock

    async def set_clock(self, iso: str) -> None:
        self._clock = iso


COLUMNS = ("id, username, display_name, password_hash, profile, starting_cash, limits, "
           "events, transcript")  # fmt: skip


def _row(row: tuple[Any, ...]) -> Trader:
    return Trader(int(row[0]), row[1], row[2], row[3], row[4], float(row[5]), row[6],
                  list(row[7] or []), list(row[8] or []))  # fmt: skip


class PostgresTraderStore:
    """`traders` and `desk_state` tables (migration 0004). Owns one connection."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._conn: psycopg.AsyncConnection[Any] | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "PostgresTraderStore":
        self._conn = await psycopg.AsyncConnection.connect(self.url, autocommit=True)
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None, tb: TracebackType | None) -> None:  # fmt: skip
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _connection(self) -> psycopg.AsyncConnection[Any]:
        if self._conn is None:
            raise RuntimeError("trader store is not connected")
        return self._conn

    async def _one(self, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
        async with self._lock:
            cursor = await self._connection().execute(sql, params)
            row: tuple[Any, ...] | None = await cursor.fetchone()
        return row

    async def create(self, username: str, password_hash: str, display_name: str,
                     profile: str, starting_cash: float) -> Trader:  # fmt: skip
        try:
            row = await self._one(
                f"insert into traders (username, display_name, password_hash, profile, "  # noqa: S608
                f"starting_cash) values (%s, %s, %s, %s, %s) returning {COLUMNS}",
                (username.lower(), display_name, password_hash, profile, starting_cash),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise UsernameTakenError(f"{username} is taken") from exc
        if row is None:
            raise RuntimeError("trader insert returned nothing")
        return _row(row)

    async def by_username(self, username: str) -> Trader | None:
        row = await self._one(f"select {COLUMNS} from traders where username = %s",  # noqa: S608
                              (username.lower(),))  # fmt: skip
        return _row(row) if row else None

    async def by_id(self, trader_id: int) -> Trader | None:
        row = await self._one(f"select {COLUMNS} from traders where id = %s", (trader_id,))  # noqa: S608
        return _row(row) if row else None

    async def list_all(self) -> list[Trader]:
        async with self._lock:
            cursor = await self._connection().execute(
                f"select {COLUMNS} from traders order by id"  # noqa: S608
            )
            rows = await cursor.fetchall()
        return [_row(row) for row in rows]

    async def save_state(self, trader_id: int, *, events: list[dict[str, Any]],
                         transcript: list[dict[str, Any]],
                         limits: dict[str, Any] | None) -> None:  # fmt: skip
        async with self._lock:
            await self._connection().execute(
                "update traders set events = %s, transcript = %s, limits = %s where id = %s",
                (Jsonb(json.loads(json.dumps(events, default=str))),
                 Jsonb(json.loads(json.dumps(transcript, default=str))),
                 Jsonb(limits) if limits is not None else None, trader_id),
            )  # fmt: skip

    async def get_clock(self) -> str | None:
        row = await self._one("select value from desk_state where key = 'clock'", ())
        return str(row[0]) if row else None

    async def set_clock(self, iso: str) -> None:
        async with self._lock:
            await self._connection().execute(
                "insert into desk_state (key, value) values ('clock', %s) on conflict (key) "
                "do update set value = excluded.value, updated_at = now()",
                (iso,),
            )
