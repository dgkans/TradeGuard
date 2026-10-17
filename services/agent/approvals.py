"""Wait for a human decision on an escalated call, through the gateway's operator tool."""

import asyncio
import time
from typing import Any

from services.agent.tools import McpToolset

WAITING_STATUSES = ("pending", "approved", "executing")


async def wait_for_decision(
    toolset: McpToolset,
    approval_id: str,
    operator_meta: dict[str, Any],
    *,
    poll_seconds: float = 2.0,
    max_wait_seconds: float = 600.0,
) -> dict[str, Any]:
    """Poll `resolve_approval` until the approval is executed, blocked, rejected or expired.

    Returns the gateway's final outcome. An approved call is executed by the gateway during
    one of these polls. Raises TimeoutError if `max_wait_seconds` passes first (normally the
    gateway's own approval timeout expires the approval before that).
    """
    deadline = time.monotonic() + max_wait_seconds
    while True:
        outcome: dict[str, Any] = await toolset.call(
            "resolve_approval", {"approval_id": approval_id}, operator_meta
        )
        if outcome.get("status") not in WAITING_STATUSES:
            return outcome
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no decision on {approval_id} after {max_wait_seconds}s")
        await asyncio.sleep(poll_seconds)
