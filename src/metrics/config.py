"""Runtime settings from environment variables."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    instance_dir: Path
    slack_bot_token: str | None
    slack_list_id: str | None


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+psycopg://metrics:metrics@localhost:5432/metrics"
        ),
        instance_dir=Path(os.environ.get("INSTANCE_DIR", "instance")),
        slack_bot_token=os.environ.get("SLACK_BOT_TOKEN") or None,
        slack_list_id=os.environ.get("SLACK_LIST_ID") or None,
    )
