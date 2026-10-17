"""Does the user's own message support this write call?

Deterministic and deliberately simple: the side must be asked for (a buy or sell word that
isn't negated), and the symbol must be named (ticker or company name). F8 adds amounts,
units and conditions.
"""

import re
from dataclasses import dataclass
from typing import Literal

from services.gateway.pipeline.types import ToolCall

SIDE_WORDS: dict[str, list[str]] = {
    "buy": ["buy", "buying", "purchase", "acquire", "add", "long", "go long", "pick up",
            "get me", "invest in", "open a position"],
    "sell": ["sell", "selling", "trim", "liquidate", "exit", "close", "close out", "dump",
             "reduce", "take profit", "take profits", "cut", "get out of", "unload"],
}  # fmt: skip
CANCEL_WORDS = ["cancel", "pull", "withdraw", "kill"]
NEGATIONS = {"don't", "dont", "do not", "never", "no", "not", "without", "stop"}
SYMBOL_ALIASES: dict[str, list[str]] = {
    "NVDA": ["nvidia"],
    "AMD": ["advanced micro devices"],
    "AAPL": ["apple"],
    "MSFT": ["microsoft"],
    "AMZN": ["amazon"],
    "META": ["meta", "facebook"],
    "GOOGL": ["google", "alphabet"],
    "TSLA": ["tesla"],
    "SPY": ["s&p 500", "s&p"],
    "QQQ": ["nasdaq 100", "nasdaq-100"],
}

Level = Literal["full", "partial", "none"]


@dataclass(frozen=True)
class Support:
    level: Level
    side_ok: bool
    symbol_ok: bool
    explanation: str


def mentions(text: str, phrase: str) -> bool:
    """True if `phrase` appears as whole words and is not negated just before it."""
    for match in re.finditer(rf"(?<![\w$]){re.escape(phrase)}(?!\w)", text):
        before = text[max(0, match.start() - 20) : match.start()].split()
        window = " ".join(before[-3:])
        negated = False
        for negation in NEGATIONS:
            if re.search(rf"(^|\s){re.escape(negation)}($|\s)", window):
                negated = True
        if not negated:
            return True
    return False


def side_requested(text: str, side: str) -> bool:
    for word in SIDE_WORDS.get(side, []):
        if mentions(text, word):
            return True
    return False


def symbol_named(text: str, symbol: str) -> bool:
    names = [symbol.lower(), f"${symbol.lower()}", *SYMBOL_ALIASES.get(symbol, [])]
    for name in names:
        if re.search(rf"(?<![\w]){re.escape(name)}(?!\w)", text):
            return True
    return False


def assess_support(user_text: str, call: ToolCall) -> Support:
    """Compare the latest user message with a write call. Never raises."""
    text = user_text.lower()
    if call.tool == "cancel_order":
        ok = any(mentions(text, word) for word in CANCEL_WORDS)
        level: Level = "full" if ok else "none"
        return Support(level, ok, ok, "user asked to cancel" if ok else "no cancel request")

    side = str(call.arg("side", "")).lower()
    symbol = str(call.arg("symbol", "")).strip().upper()
    side_ok = side_requested(text, side)
    symbol_ok = symbol_named(text, symbol)
    if side_ok and symbol_ok:
        return Support("full", True, True, f"user asked to {side} {symbol}")
    if side_ok:
        return Support("partial", True, False,
                       f"user asked to {side} but did not name {symbol}")  # fmt: skip
    return Support("none", False, symbol_ok, f"user did not ask to {side} {symbol}")
