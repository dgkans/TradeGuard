"""Deterministic parse of a user's trading instruction.

The output is a list of intents `{side, symbol, amount, unit, condition}` and an
`ambiguous` flag. Ambiguous means the deterministic rules can't safely say what the user
wants, and the LLM judge should look. It is set for: a bare number with no unit ("buy 1000
NVDA"), vague quantities ("a little", "double"), a pronoun or missing target ("buy more of
it", "buy $500"), and trade verbs outside the side vocabulary ("put $500 into", "load up").
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from services.gateway.provenance.support import NEGATIONS, SYMBOL_ALIASES

Side = Literal["buy", "sell"]
Unit = Literal["usd", "shares", "pct_position"]

SIDE_PHRASES: dict[str, list[str]] = {
    "buy": ["buy", "buying", "purchase", "acquire", "add", "go long", "invest in", "pick up"],
    "sell": ["sell", "selling", "trim", "liquidate", "exit", "close out", "close", "dump",
             "reduce", "cut", "unload", "take profits", "take profit", "take some profit"],
}  # fmt: skip
OTHER_TRADE_VERBS = re.compile(
    r"\b(put|load up|load|move|rotate|swap|switch|all in|grab|get rid of|double|triple|"
    r"scale (in|out)|top up)\b"
)
VAGUE = re.compile(
    r"\b(a little|a bit|some more|more of|some|a few|most of|the rest|whatever|my best|"
    r"my worst|my losers|my winners|half my cash|a chunk)\b"
)
PRONOUN_TARGET = re.compile(r"\b(it|that|them|this one|those)\b")
DELEGATION = re.compile(
    r"\b(strongest|whichever|top|best|biggest)\s+(\w+\s+){0,3}"
    r"(breakout|setup|stock|one|name|pattern|signal|mover)s?\b|\bbreaking out\b|\bwatchlist\b"
)
CONDITION = re.compile(r"\b(if|when|once|after|unless|as soon as)\b[^,;]*")
KNOWN_TICKERS = [
    "NVDA", "AMD", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "SPY", "QQQ",
    "NFLX", "AVGO", "JPM", "XOM", "COIN", "PLTR", "INTC", "BA", "DIS", "IWM",
]  # fmt: skip
NUMBER = r"(\d[\d,]*(?:\.\d+)?)"
AMOUNT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("usd", re.compile(r"\$\s?" + NUMBER + r"\s?(k|m)?\b")),
    ("usd", re.compile(NUMBER + r"\s?(k|m)?\s?(dollars|usd|bucks)\b")),
    ("shares", re.compile(NUMBER + r"\s?(shares?|shs)\b")),
    ("pct_position", re.compile(NUMBER + r"\s?%")),
]
WORD_PCT: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"\b(all|everything|entire|whole|full position)\b"), 100.0),
    (re.compile(r"\bhalf\b"), 50.0),
    (re.compile(r"\ba third\b"), 100.0 / 3),
    (re.compile(r"\ba quarter\b"), 25.0),
]
BARE_NUMBER = re.compile(r"(?<![\w$.,%])" + NUMBER + r"(?![\w%.,])")


@dataclass(frozen=True)
class Intent:
    side: Side | None
    symbol: str | None
    amount: float | None
    unit: Unit | None
    condition: str | None = None
    delegated: bool = False  # the user let the agent pick the symbol


@dataclass(frozen=True)
class ParseResult:
    intents: list[Intent]
    ambiguous: bool
    reasons: list[str] = field(default_factory=list)


def to_number(raw: str, suffix: str | None) -> float:
    value = float(raw.replace(",", ""))
    if suffix == "k":
        value *= 1000
    elif suffix == "m":
        value *= 1_000_000
    return value


def side_matches(text: str) -> list[tuple[int, int, Side]]:
    """(start, end, side) of every side phrase that isn't negated, in order."""
    found: list[tuple[int, int, Side]] = []
    for side, phrases in SIDE_PHRASES.items():
        for phrase in sorted(phrases, key=len, reverse=True):
            for match in re.finditer(rf"(?<!\w){re.escape(phrase)}(?!\w)", text):
                if overlaps(found, match.start(), match.end()) or negated(text, match.start()):
                    continue
                found.append((match.start(), match.end(), side))  # type: ignore[arg-type]
    return sorted(found)


def overlaps(spans: list[tuple[int, int, Side]], start: int, end: int) -> bool:
    for other_start, other_end, _ in spans:
        if start < other_end and other_start < end:
            return True
    return False


def negated(text: str, start: int) -> bool:
    window = " ".join(text[max(0, start - 20) : start].split()[-3:])
    for negation in NEGATIONS:
        if re.search(rf"(^|\s){re.escape(negation)}($|\s)", window):
            return True
    return False


def symbol_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for ticker in KNOWN_TICKERS:
        names = [ticker.lower(), *SYMBOL_ALIASES.get(ticker, [])]
        for name in names:
            for match in re.finditer(rf"(?<!\w)\$?{re.escape(name)}(?!\w)", text):
                spans.append((match.start(), match.end(), ticker))
    spans.sort()
    unique: list[tuple[int, int, str]] = []
    for span in spans:
        if unique and span[0] < unique[-1][1]:
            continue
        unique.append(span)
    return unique


def mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


def find_amount(region: str) -> tuple[float | None, Unit | None, bool]:
    """(amount, unit, bare) for the first amount in `region`. `bare` = number, no unit."""
    best: tuple[int, float, Unit] | None = None
    for unit, pattern in AMOUNT_PATTERNS:
        match = pattern.search(region)
        if match and (best is None or match.start() < best[0]):
            suffix = (
                match.group(2)
                if unit == "usd" and match.lastindex and match.lastindex >= 2
                else None
            )
            value = to_number(match.group(1), suffix if suffix in ("k", "m") else None)
            best = (match.start(), value, unit)  # type: ignore[assignment]
    if best is not None:
        return best[1], best[2], False
    for pattern, pct in WORD_PCT:
        if pattern.search(region):
            return pct, "pct_position", False
    bare = BARE_NUMBER.search(region)
    if bare:
        return to_number(bare.group(1), None), None, True
    return None, None, False


def parse_intent(text: str) -> ParseResult:
    """Parse the latest user message. Pure and deterministic; never raises."""
    lowered = text.lower()
    reasons: list[str] = []
    sides = side_matches(lowered)
    other_verbs = OTHER_TRADE_VERBS.search(lowered)
    if not sides:
        if other_verbs:
            return ParseResult(
                [], True, [f"trade verb '{other_verbs.group(0)}' is not a clear buy or sell"]
            )
        return ParseResult([], False, [])
    if other_verbs:
        reasons.append(f"trade verb '{other_verbs.group(0)}' is not a clear buy or sell")
    vague = VAGUE.search(lowered)
    if vague:
        reasons.append(f"vague quantity or target '{vague.group(0)}'")

    intents: list[Intent] = []
    for index, (start, _end, side) in enumerate(sides):
        stop = sides[index + 1][0] if index + 1 < len(sides) else len(lowered)
        intents.extend(parse_segment(lowered, start, stop, side, reasons))
    return ParseResult(intents, bool(reasons), reasons)


def parse_segment(text: str, start: int, stop: int, side: Side, reasons: list[str]) -> list[Intent]:
    segment = text[start:stop]
    condition_match = CONDITION.search(segment)
    condition = condition_match.group(0).strip() if condition_match else None
    body_end = condition_match.start() if condition_match else len(segment)
    symbols = [s for s in symbol_spans(segment) if s[0] < body_end]
    masked = mask(segment[:body_end], [(s[0], s[1]) for s in symbols])

    if not symbols:
        amount, unit, bare = find_amount(masked)
        delegated = bool(DELEGATION.search(segment))
        if not delegated:
            target = "a pronoun" if PRONOUN_TARGET.search(segment) else "no symbol"
            reasons.append(f"'{segment.strip()}' names {target}")
        if bare:
            reasons.append(f"'{format_number(amount)}' has no unit (dollars or shares?)")
        return [Intent(side, None, amount, unit, condition, delegated)]

    intents = []
    for i, (sym_start, sym_end, ticker) in enumerate(symbols):
        prev_end = symbols[i - 1][1] if i > 0 else 0
        next_start = symbols[i + 1][0] if i + 1 < len(symbols) else body_end
        amount, unit, bare = find_amount(masked[prev_end:sym_start])
        if amount is None:
            amount, unit, bare = find_amount(masked[sym_end:next_start])
        if bare:
            reasons.append(f"'{format_number(amount)}' has no unit (dollars or shares?)")
        intents.append(Intent(side, ticker, amount, unit, condition))
    return intents


def format_number(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:g}" if value < 1e6 else f"{value:,.0f}"
