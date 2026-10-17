"""Report the user's own message to the gateway, as trusted provenance."""

from typing import Any

from services.agent.tools import McpToolset


async def report_user_message(
    toolset: McpToolset, text: str, operator_meta: dict[str, Any]
) -> None:
    """Call the gateway's operator tool before each turn. Raises RuntimeError on refusal.

    This runs in the CLI's code with the operator token, never as a model tool call, so the
    model cannot invent a "user message" to justify an order.
    """
    await toolset.call("record_user_message", {"text": text}, operator_meta)
