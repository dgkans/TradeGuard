"""Run the Alpaca paper adapter on its own, over stdio (for the MCP Inspector).

    TRADING_MODE=paper uv run python -m services.alpaca_adapter

The gateway does not need this: `--upstream alpaca` runs the adapter in process. Refuses
to start unless paper mode is configured and the account is a paper account.
"""

import asyncio
import sys

from services.alpaca_adapter.adapter import PaperModeError, alpaca_server_params, connect_adapter
from services.common.config import ConfigError, load_settings


async def serve() -> int:
    params = alpaca_server_params(load_settings())
    async with connect_adapter(params) as server:
        print("alpaca_adapter: paper account verified, serving on stdio", file=sys.stderr)
        await server.run_stdio_async()
    return 0


def main() -> int:
    try:
        return asyncio.run(serve())
    except (ConfigError, PaperModeError, OSError) as exc:
        print(f"alpaca_adapter: refusing to start: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
