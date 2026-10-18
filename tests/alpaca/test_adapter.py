"""Alpaca adapter: same tool contract as the replay server, paper mode only."""

from typing import Any

import pytest
from mcp import Client

from services.alpaca_adapter.adapter import (
    ALPACA_MCP_PACKAGE,
    PaperModeError,
    alpaca_server_params,
    connect_adapter,
    require_paper_settings,
)
from services.common.config import Settings, load_settings
from tests.alpaca.fake_alpaca import FakeAlpaca, build_fake_server

KEYS = {"TRADING_MODE": "paper", "ALPACA_API_KEY": "PKTESTKEY123", "ALPACA_SECRET_KEY": "s3cret"}


def settings(**env: str) -> Settings:
    return load_settings(env_file=None, environ={**KEYS, **env})


async def call(state: FakeAlpaca, tool: str, args: dict[str, Any] | None = None) -> Any:
    async with connect_adapter(build_fake_server(state)) as server, Client(server) as client:
        result = await client.call_tool(tool, args or {})
    assert not result.is_error, result.content
    return result.structured_content


async def test_account_is_translated_and_masked() -> None:
    account = await call(FakeAlpaca(), "get_account")
    assert account == {
        "account_id": "paper-****CT01",
        "mode": "paper",
        "currency": "USD",
        "cash": 98000.5,
        "buying_power": 196001.0,
        "positions_value": 2000.0,
        "equity": 100000.5,
    }


async def test_positions_quote_clock_and_bars_have_the_replay_shapes() -> None:
    state = FakeAlpaca()
    positions = await call(state, "get_positions")
    assert positions == {"positions": [{"symbol": "NVDA", "qty": 10.0, "avg_entry_price": 175.5,
                                        "market_price": 180.25, "market_value": 1802.5,
                                        "unrealized_pl": 47.5}]}  # fmt: skip
    quote = await call(state, "get_quote", {"symbol": "nvda"})
    assert quote == {"symbol": "NVDA", "price": 180.25,
                     "as_of": "2026-10-16T14:35:12.123456+00:00", "market_open": True}  # fmt: skip
    clock = await call(state, "get_clock")
    assert clock == {"now": "2026-10-16T14:35:12.123456+00:00", "market_open": True}
    bars = await call(state, "get_bars", {"symbol": "NVDA", "interval": "1d", "limit": 2})
    assert bars["symbol"] == "NVDA" and bars["interval"] == "1d"
    assert bars["bars"][1] == {"symbol": "NVDA", "start": "2026-10-15T04:00:00+00:00",
                               "open": 175.0, "high": 181.0, "low": 174.0, "close": 180.0,
                               "volume": 1200}  # fmt: skip
    sent = next(args for tool, args in state.calls if tool == "get_stock_bars")
    assert sent["timeframe"] == "1Day" and sent["days"] == 5
    # Ask Alpaca for more than needed and keep the newest `limit` (it counts from the start).
    assert sent["limit"] > 2 and len(bars["bars"]) == 2


async def test_news_is_mapped_to_items() -> None:
    news = await call(FakeAlpaca(), "get_news", {"symbol": "NVDA", "limit": 3})
    assert news["items"] == [{"id": "alpaca-4211", "symbols": ["NVDA"],
                              "published_at": "2026-10-16T13:00:00+00:00", "source": "benzinga",
                              "headline": "Chipmakers rally", "body": "Shares rose."}]  # fmt: skip


async def test_place_list_and_cancel_an_order() -> None:
    state = FakeAlpaca()
    async with connect_adapter(build_fake_server(state)) as server, Client(server) as client:
        placed = (await client.call_tool("place_order", {
            "symbol": "NVDA", "side": "buy", "qty": 1, "order_type": "limit",
            "limit_price": 1.0})).structured_content  # fmt: skip
        listed = (await client.call_tool("list_orders", {"status": "open"})).structured_content
        canceled = await client.call_tool("cancel_order", {"order_id": placed["id"]})
    assert placed["symbol"] == "NVDA" and placed["status"] == "accepted"
    assert placed["qty"] == 1.0 and placed["limit_price"] == 1.0 and placed["type"] == "limit"
    assert placed["submitted_at"] == "2026-10-16T14:35:12.123456+00:00"
    sent = next(args for tool, args in state.calls if tool == "place_stock_order")
    assert sent == {"symbol": "NVDA", "side": "buy", "qty": "1", "notional": None,
                    "type": "limit", "time_in_force": "day", "limit_price": "1"}  # fmt: skip
    assert [o["id"] for o in listed["orders"]] == [placed["id"]]
    assert canceled.structured_content["status"] == "canceled"


async def test_notional_market_order_is_sent_as_notional() -> None:
    state = FakeAlpaca()
    placed = await call(state, "place_order", {"symbol": "AMD", "side": "buy", "notional": 300})
    assert placed["notional"] == 300.0 and placed["qty"] is None
    sent = next(args for tool, args in state.calls if tool == "place_stock_order")
    assert sent["notional"] == "300" and sent["qty"] is None and sent["type"] == "market"


async def test_bad_order_arguments_are_refused_before_reaching_alpaca() -> None:
    state = FakeAlpaca()
    async with connect_adapter(build_fake_server(state)) as server, Client(server) as client:
        both = await client.call_tool("place_order", {"symbol": "NVDA", "side": "buy",
                                                      "qty": 1, "notional": 100})  # fmt: skip
        stop = await client.call_tool("place_order", {"symbol": "NVDA", "side": "buy",
                                                      "qty": 1, "order_type": "stop"})  # fmt: skip
    assert both.is_error and stop.is_error
    assert not [tool for tool, _ in state.calls if tool == "place_stock_order"]


async def test_adapter_refuses_a_non_paper_account() -> None:
    with pytest.raises(PaperModeError, match="not a paper account"):
        async with connect_adapter(build_fake_server(FakeAlpaca(account_number="312345678"))):
            pass


async def test_only_replay_tool_names_are_exposed() -> None:
    async with connect_adapter(build_fake_server(FakeAlpaca())) as server, Client(server) as c:
        names = {tool.name for tool in (await c.list_tools()).tools}
    assert names == {"get_clock", "get_quote", "get_bars", "get_news", "get_account",
                     "get_positions", "list_orders", "place_order", "cancel_order"}  # fmt: skip


async def test_bad_keys_refuse_cleanly_instead_of_crashing() -> None:
    """Regression: a 401 while verifying the account escaped as an ExceptionGroup traceback."""
    with pytest.raises(PaperModeError, match="could not verify"):
        async with connect_adapter(build_fake_server(FakeAlpaca(bad_keys=True))):
            pass


# --- paper mode only ---------------------------------------------------------------------


def test_official_server_env_is_built_from_scratch_and_forces_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "false")  # must never leak through
    monkeypatch.setenv("SOME_OTHER_SECRET", "x")
    params = alpaca_server_params(settings())
    assert params.command == "uvx"
    assert params.args == [ALPACA_MCP_PACKAGE]
    assert ALPACA_MCP_PACKAGE == "alpaca-mcp-server==2.3.2"
    env = params.env or {}
    assert env["ALPACA_PAPER_TRADE"] == "true"
    assert env["ALPACA_API_KEY"] == "PKTESTKEY123"
    assert env["ALPACA_SECRET_KEY"] == "s3cret"  # noqa: S105 - test value
    assert env["ALPACA_TOOLSETS"] == "account,trading,assets,stock-data,news"
    assert set(env) <= {"ALPACA_PAPER_TRADE", "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
                        "ALPACA_TOOLSETS", "PATH", "HOME", "UV_CACHE_DIR"}  # fmt: skip


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"TRADING_MODE": "replay"}, "TRADING_MODE=paper"),
        ({"ALPACA_API_KEY": ""}, "ALPACA_API_KEY"),
        ({"ALPACA_SECRET_KEY": ""}, "ALPACA_SECRET_KEY"),
    ],
)
def test_adapter_needs_explicit_paper_mode_and_keys(env: dict[str, str], message: str) -> None:
    with pytest.raises(PaperModeError, match=message):
        require_paper_settings(settings(**env))


@pytest.mark.parametrize(
    "env",
    [
        {"ALPACA_PAPER": "false"},
        {"ALPACA_BASE_URL": "https://api.alpaca.markets"},
        {"TRADING_MODE": "live"},
        {"LIVE_TRADING": "true"},
    ],
)
def test_live_mode_cannot_even_be_loaded(env: dict[str, str]) -> None:
    from services.common.config import ConfigError

    with pytest.raises(ConfigError):
        settings(**env)
