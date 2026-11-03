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
    team_password_hash: str | None = None
    session_secret: str | None = None
    auth_mode: str = "password"
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_allowed_group: str | None = None
    oidc_groups_claim: str = "groups"
    public_url: str | None = None


AUTH_MODES = ("password", "oidc", "off")


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+psycopg://metrics:metrics@localhost:5432/metrics"
        ),
        instance_dir=Path(os.environ.get("INSTANCE_DIR", "instance")),
        slack_bot_token=os.environ.get("SLACK_BOT_TOKEN") or None,
        slack_list_id=os.environ.get("SLACK_LIST_ID") or None,
        team_password_hash=os.environ.get("TEAM_PASSWORD_HASH") or None,
        session_secret=os.environ.get("SESSION_SECRET") or None,
        auth_mode=_auth_mode(),
        oidc_issuer=os.environ.get("OIDC_ISSUER") or None,
        oidc_client_id=os.environ.get("OIDC_CLIENT_ID") or None,
        oidc_client_secret=os.environ.get("OIDC_CLIENT_SECRET") or None,
        oidc_allowed_group=os.environ.get("OIDC_ALLOWED_GROUP") or None,
        oidc_groups_claim=os.environ.get("OIDC_GROUPS_CLAIM") or "groups",
        public_url=(os.environ.get("PUBLIC_URL") or "").rstrip("/") or None,
    )


def _auth_mode() -> str:
    value = (os.environ.get("AUTH_MODE") or "password").strip().lower()
    if value not in AUTH_MODES:
        # Fail loudly instead of guessing: a typo must never open the analysis page.
        raise ValueError(f"AUTH_MODE must be one of {', '.join(AUTH_MODES)}, not {value!r}")
    return value
