"""Deterministic intent parsing."""

import pytest

from services.gateway.intent.parser import parse_intent


def only(text: str) -> tuple[str | None, str | None, float | None, str | None]:
    parsed = parse_intent(text)
    assert len(parsed.intents) == 1, parsed
    intent = parsed.intents[0]
    return intent.side, intent.symbol, intent.amount, intent.unit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Buy $500 of NVDA", ("buy", "NVDA", 500.0, "usd")),
        ("buy 1,000 dollars of nvidia", ("buy", "NVDA", 1000.0, "usd")),
        ("Buy $1.5k of TSLA", ("buy", "TSLA", 1500.0, "usd")),
        ("buy 2 shares of AMD", ("buy", "AMD", 2.0, "shares")),
        ("Sell 3 shares of NVDA", ("sell", "NVDA", 3.0, "shares")),
        ("Trim half of my NVDA", ("sell", "NVDA", 50.0, "pct_position")),
        ("Sell all my Apple", ("sell", "AAPL", 100.0, "pct_position")),
        ("Cut my NVDA exposure by 30%", ("sell", "NVDA", 30.0, "pct_position")),
        ("sell NVDA", ("sell", "NVDA", None, None)),
    ],
)
def test_clear_instructions(text: str, expected: tuple[object, ...]) -> None:
    assert only(text) == expected
    assert parse_intent(text).ambiguous is False


def test_bare_number_is_ambiguous_unit() -> None:
    parsed = parse_intent("buy 1000 NVDA")
    assert parsed.ambiguous is True
    assert "unit" in " ".join(parsed.reasons)
    assert parsed.intents[0].amount == 1000.0
    assert parsed.intents[0].unit is None


def test_condition_is_captured() -> None:
    parsed = parse_intent("Buy $500 of NVDA if it breaks yesterday's high")
    assert parsed.intents[0].condition == "if it breaks yesterday's high"
    assert parsed.ambiguous is False


def test_two_intents_in_one_message() -> None:
    parsed = parse_intent("buy $500 of NVDA and $300 of AMD")
    assert [(i.symbol, i.amount, i.unit) for i in parsed.intents] == [
        ("NVDA", 500.0, "usd"),
        ("AMD", 300.0, "usd"),
    ]


def test_delegated_symbol_is_not_ambiguous() -> None:
    parsed = parse_intent("Scan my watchlist and buy $500 of the strongest breakout")
    intent = parsed.intents[0]
    assert (intent.side, intent.symbol, intent.delegated) == ("buy", None, True)
    assert parsed.ambiguous is False


@pytest.mark.parametrize(
    "text",
    [
        "Put a little money into AMD",
        "Double my Apple position",
        "buy more of it",
        "Load up on AAPL",
        "buy $500",
    ],
)
def test_vague_instructions_are_ambiguous(text: str) -> None:
    assert parse_intent(text).ambiguous is True


@pytest.mark.parametrize(
    "text",
    ["What's NVDA trading at?", "Don't sell my NVDA", "Check the latest news on NVDA", ""],
)
def test_no_trade_intent(text: str) -> None:
    parsed = parse_intent(text)
    assert parsed.intents == []
    assert parsed.ambiguous is False
