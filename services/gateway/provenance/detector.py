"""Heuristic prompt injection signals for untrusted text.

This only produces a risk score; the taint rule makes the decision (PLAN.md F7). Each
signal has a weight, and the score is 1 - prod(1 - weight) over the signals that fired,
so independent weak signals add up and the score stays in [0, 1].

These are general patterns of instructions aimed at a model (override phrases, chat role
markers, addressing an AI, urgency next to a trade verb, secrecy, claimed authority). They
were not fitted to the labeled evaluation set (evals/datasets/injection_snippets.jsonl).
"""

import re
from dataclasses import dataclass

THRESHOLD = 0.5
FLAGS = re.IGNORECASE | re.MULTILINE

TRADE_VERBS = (
    r"(sell|buy|purchase|liquidate|dump|short|cancel|close|transfer|wire|rebalance|execute|"
    r"place (an? )?(market |limit )?(buy |sell )?orders?)"
)
URGENCY = r"(immediately|urgent(ly)?|right now|asap|at once|act now|without delay|now\b|today)"
AI_NOUNS = (
    r"(ai|llm|language model|model|assistant|agent|agents|bot|bots|trading bot|trading software|"
    r"autonomous trading software|automated agent|automated trading)"
)

SIGNALS: list[tuple[str, float, re.Pattern[str]]] = [
    (
        "instruction_override",
        0.6,
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass|disable)\b[^.\n]{0,40}"
            r"\b(instructions?|rules|prompt|limits?|everything (above|before)|request|safety|"
            r"checks?|polic(y|ies)|approval)\b"
            r"|\b(previous|prior|old) (rules|instructions) (are|is) (void|cancelled|invalid)"
            r"|\byour (new|real|actual) (instructions?|objective|task|goal)s?\b"
            r"|\bnew (instructions?|task|policy) (for|to) (the )?" + AI_NOUNS + r"\b"
            r"|\bthis overrides\b"
            r"|\b(ignora|ignorez|ignoriere|ignore as)\b[^.\n]{0,30}\b(instrucciones|instructions|"
            r"anweisungen|instruções)\b",
            FLAGS,
        ),
    ),
    (
        "role_marker",
        0.5,
        re.compile(
            r"(^|\n)\s*(system|assistant|user|developer)\s*:"
            r"|\bsystem (override|prompt|message|notice|instruction)\b"
            r"|\[/?inst\]|<\|im_(start|end)\|>|</?system>|^\s*#+\s*instruction"
            r"|-{2,}\s*end of (article|document|message)\s*-{2,}",
            FLAGS,
        ),
    ),
    (
        "addresses_ai",
        0.35,
        re.compile(
            r"\b(dear|hey|hello|attention|note to|reminder to|to any|to the|for|instruction to|"
            r"p\.?s\.? to the|if you are an?)\s+(\w+[\s-]+){0,3}" + AI_NOUNS + r"\b"
            r"|\b" + AI_NOUNS + r"\s*(,|:)\s*(please|you|go|act|your|execute|sell|buy)\b"
            r"|\byou are (now|dan)\b|\bany (llm|bot|agent|ai)\b",
            FLAGS,
        ),
    ),
    (
        "urgency_trade",
        0.3,
        re.compile(
            URGENCY
            + r"[^.\n]{0,50}\b"
            + TRADE_VERBS
            + r"\b|\b"
            + TRADE_VERBS
            + r"\b[^.\n]{0,50}"
            + URGENCY,
            FLAGS,
        ),
    ),
    (
        "imperative_trade",
        0.3,
        re.compile(
            r"(^|[.!:;]\s+|\n|\bto\s+)(please\s+|now\s+|quietly\s+|go ahead and\s+)?"
            r"(sell|buy|liquidate|dump|purchase|short|cancel|close|transfer|wire|execute|place)"
            r"\s+(all|every|everything|the|as much|\$?\d|a market|market|sell|buy|orders?)\b",
            FLAGS,
        ),
    ),
    (
        "secrecy",
        0.45,
        re.compile(
            r"\b(do not|don't|dont|never)\s+(tell|mention|inform|notify|ask|double[- ]check|"
            r"confirm with)\b|\bwithout (telling|asking|informing|further confirmation|"
            r"confirmation)\b|\bkeep (it|this) (secret|quiet)\b|\b(silently|quietly)\b"
            r"|\bbefore (they|the user) (wake|notice)|\bno questions\b|\bno time to ask\b"
            r"|\bnothing happened\b",
            FLAGS,
        ),
    ),
    (
        "authority_claim",
        0.35,
        re.compile(
            r"\bas your (administrator|admin|operator|developer|owner)\b|\bi authori[sz]e you\b"
            r"|\b(pre-?authori[sz]ed|has authori[sz]ed you)\b|\bdirect command from\b"
            r"|\b(the )?(user|account owner|operator) (has |)(asked|wants|says|instructed)"
            r"[^.\n]{0,40}\byou\b|\bmust place\b|\bas part of compliance\b",
            FLAGS,
        ),
    ),
]


@dataclass(frozen=True)
class Detection:
    score: float
    signals: list[str]

    @property
    def flagged(self) -> bool:
        return self.score >= THRESHOLD


def detect(text: str) -> Detection:
    """Score untrusted text for injection signals. Pure, deterministic, never raises."""
    fired = []
    keep = 1.0
    for name, weight, pattern in SIGNALS:
        if pattern.search(text):
            fired.append(name)
            keep *= 1.0 - weight
    return Detection(score=round(1.0 - keep, 4), signals=fired)
