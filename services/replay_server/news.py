"""Synthetic news fixtures. All of it is untrusted free text from an external source."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    symbols: list[str]
    published_at: datetime
    source: str
    headline: str
    body: str
    # Eval label: does this item carry a prompt injection payload? Never sent to the agent.
    injection: bool = False
    eval_notes: str = Field(default="", description="Eval only. Never sent to the agent.")

    def public_view(self) -> dict[str, Any]:
        """What the agent may see. Drops eval labels and marks the item untrusted."""
        return {
            "id": self.id,
            "symbols": list(self.symbols),
            "published_at": self.published_at.isoformat(),
            "source": self.source,
            "headline": self.headline,
            "body": self.body,
            "trusted": False,
            "synthetic": True,
        }


def load_news(root: Path, ids: list[str]) -> list[NewsItem]:
    """Load the listed fixture ids from `root/<id>.json`.

    Raises FileNotFoundError naming the id when a fixture is missing, so a scenario typo
    fails at startup instead of silently showing no news.
    """
    items = []
    for news_id in ids:
        path = root / f"{news_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"news fixture {news_id!r} not found at {path}")
        items.append(NewsItem.model_validate(json.loads(path.read_text())))
    return items
