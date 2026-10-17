"""News fixtures. All of it is synthetic and untrusted free text from an external source.

Synthetic and trust labels live in the fixture files (`synthetic`, `trusted`, `injection`,
`eval_notes`) and are never shown to the agent: the agent sees news the way an ordinary
feed would show it (DECISIONS.md D013). Trust handling is the gateway's job.
"""

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
    # Labels for humans and evals. None of these are sent to the agent.
    synthetic: bool = True
    trusted: bool = False
    injection: bool = False
    eval_notes: str = Field(default="", description="Eval only. Never sent to the agent.")

    def public_view(self) -> dict[str, Any]:
        """What the agent may see: the item as a news feed would show it."""
        return {
            "id": self.id,
            "symbols": list(self.symbols),
            "published_at": self.published_at.isoformat(),
            "source": self.source,
            "headline": self.headline,
            "body": self.body,
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
