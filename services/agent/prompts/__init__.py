"""Versioned prompt files. A prompt's version is its file name, recorded with every run."""

from pathlib import Path

PROMPT_DIR = Path(__file__).parent
SYSTEM_PROMPT = "system_v1"
UNDERSTAND_PROMPT = "understand_v1"
PLAN_PROMPT = "plan_v1"
JUDGE_PROMPT = "judge_v1"  # used by the gateway's intent judge (F8)


def load_prompt(name: str) -> str:
    """Text of `<name>.md`. Raises FileNotFoundError for an unknown version."""
    return (PROMPT_DIR / f"{name}.md").read_text()
