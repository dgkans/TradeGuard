"""Owner-only admin login (F10d). One password from settings; no users table, no database.

    ADMIN_PASSWORD=...  in .env   (unset: admin is off, every admin endpoint refuses)

A successful login returns a token `<expiry in ms>.<hmac>`, signed with a key made fresh in each
API process: tokens die when the API restarts, and can't be forged or extended without
the key. Password and signature checks are constant time. After `max_failures` wrong
passwords, logins are refused for `lock_s` seconds (even the right one).
"""

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime


class AdminLoginError(Exception):
    """Wrong password."""


class AdminLockedError(Exception):
    """Too many wrong passwords; try again later."""


class AdminDisabledError(Exception):
    """No admin password is configured."""


@dataclass(frozen=True)
class AdminToken:
    token: str
    expires_at: str


class AdminAuth:
    def __init__(
        self,
        password: str | None,
        ttl_s: float = 12 * 3600,
        max_failures: int = 5,
        lock_s: float = 300,
    ) -> None:
        self._password = password or None
        self._key = secrets.token_bytes(32)
        self.ttl_s = ttl_s
        self.max_failures = max_failures
        self.lock_s = lock_s
        self._failures = 0
        self._locked_until = 0.0

    @property
    def enabled(self) -> bool:
        return self._password is not None

    def _sign(self, expiry: int) -> str:
        return hmac.new(self._key, f"admin:{expiry}".encode(), hashlib.sha256).hexdigest()

    def login(self, password: str) -> AdminToken:
        """A token for the right password. Raises AdminDisabledError, AdminLockedError or
        AdminLoginError."""
        if self._password is None:
            raise AdminDisabledError("admin login is not configured (set ADMIN_PASSWORD)")
        now = time.monotonic()
        if now < self._locked_until:
            raise AdminLockedError("too many failed attempts; try again in a few minutes")
        if not hmac.compare_digest(password.encode(), self._password.encode()):
            self._failures += 1
            if self._failures >= self.max_failures:
                self._failures = 0
                self._locked_until = now + self.lock_s
            raise AdminLoginError("wrong password")
        self._failures = 0
        expiry_ms = int((time.time() + self.ttl_s) * 1000)
        return AdminToken(
            token=f"{expiry_ms}.{self._sign(expiry_ms)}",
            expires_at=datetime.fromtimestamp(expiry_ms / 1000, UTC).isoformat(),
        )

    def verify(self, token: str | None) -> bool:
        """True for an unexpired token signed by this process."""
        if not token or self._password is None or "." not in token:
            return False
        raw_expiry, mac = token.split(".", 1)
        if not raw_expiry.isdigit():
            return False
        expiry_ms = int(raw_expiry)
        if not hmac.compare_digest(mac, self._sign(expiry_ms)):
            return False
        return time.time() * 1000 < expiry_ms
