"""Runtime settings from environment variables."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    database_url: str
    instance_dir: Path
    slack_bot_token: str | None
    slack_list_id: str | None
    # A project's own List (e.g. JugendLab), read once by `metrics transfer-slack`.
    slack_project_list_id: str | None = None
    team_password_hash: str | None = None
    allow_password_login: bool = False
    session_secret: str | None = None
    auth_mode: str = "password"
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_allowed_group: str | None = None
    oidc_admin_group: str | None = None
    oidc_groups_claim: str = "groups"
    public_url: str | None = None
    # The public front page on an address of its own (WM-154), e.g. https://zahlen.example.org; the
    # team part stays on PUBLIC_URL. Without it the app serves everything on one address.
    front_url: str | None = None
    # The course calendar (WM-110): a CalDAV collection, e.g. a Nextcloud calendar of the app's own
    # account; without CALDAV_URL planning works without a calendar. CALDAV_ORGANIZER is the e-mail
    # address of that account: with it, lead and support are invited as attendees.
    caldav_url: str | None = None
    caldav_username: str | None = None
    caldav_password: str | None = None
    caldav_organizer: str | None = None
    # The public calendar (WM-110), same account and login: Termine of contexts marked "Im
    # öffentlichen Kalender" also appear there, without names. Without it there is only the internal one.
    caldav_public_url: str | None = None
    # Kimai Cloud (WM-142): absences and weekly hours, read hourly by the scheduler with the API token
    # of a read-only account. Without both the Kimai job does nothing. Never log the token.
    kimai_url: str | None = None
    kimai_token: str | None = None
    # Nightly dumps of the scheduler (WM-103); the only directory the scheduler writes to.
    backup_dir: Path = Path("backups/nightly")


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
        slack_project_list_id=os.environ.get("SLACK_PROJECT_LIST_ID") or None,
        team_password_hash=os.environ.get("TEAM_PASSWORD_HASH") or None,
        allow_password_login=(os.environ.get("ALLOW_PASSWORD_LOGIN") or "").strip().lower() == "true",
        session_secret=os.environ.get("SESSION_SECRET") or None,
        auth_mode=_auth_mode(),
        oidc_issuer=os.environ.get("OIDC_ISSUER") or None,
        oidc_client_id=os.environ.get("OIDC_CLIENT_ID") or None,
        oidc_client_secret=os.environ.get("OIDC_CLIENT_SECRET") or None,
        oidc_allowed_group=os.environ.get("OIDC_ALLOWED_GROUP") or None,
        oidc_admin_group=os.environ.get("OIDC_ADMIN_GROUP") or None,
        oidc_groups_claim=os.environ.get("OIDC_GROUPS_CLAIM") or "groups",
        public_url=(os.environ.get("PUBLIC_URL") or "").rstrip("/") or None,
        front_url=_front_url(),
        caldav_url=os.environ.get("CALDAV_URL") or None,
        caldav_username=os.environ.get("CALDAV_USERNAME") or None,
        caldav_password=os.environ.get("CALDAV_PASSWORD") or None,
        caldav_organizer=os.environ.get("CALDAV_ORGANIZER") or None,
        caldav_public_url=_public_calendar_url(),
        kimai_url=(os.environ.get("KIMAI_URL") or "").strip().rstrip("/") or None,
        kimai_token=(os.environ.get("KIMAI_TOKEN") or "").strip() or None,
        backup_dir=Path(os.environ.get("BACKUP_DIR") or "backups/nightly"),
    )


def _auth_mode() -> str:
    value = (os.environ.get("AUTH_MODE") or "password").strip().lower()
    if value not in AUTH_MODES:
        # Fail loudly instead of guessing: a typo must never open the analysis page.
        raise ValueError(f"AUTH_MODE must be one of {', '.join(AUTH_MODES)}, not {value!r}")
    return value


def _front_url() -> str | None:
    front = (os.environ.get("FRONT_URL") or "").strip().rstrip("/") or None
    if front is None:
        return None
    public = (os.environ.get("PUBLIC_URL") or "").strip().rstrip("/")
    # Fail loudly: on the front address only the public pages answer, so the team part needs its own.
    if not public:
        raise ValueError("FRONT_URL needs PUBLIC_URL, the address of the team part")
    if urlsplit(front).hostname == urlsplit(public).hostname:
        raise ValueError("FRONT_URL and PUBLIC_URL must have different host names")
    return front


def _public_calendar_url() -> str | None:
    public = (os.environ.get("CALDAV_PUBLIC_URL") or "").strip() or None
    internal = (os.environ.get("CALDAV_URL") or "").strip()
    # Fail loudly: in one calendar the public events would show up as events without a Termin.
    if public and public.rstrip("/") == internal.rstrip("/"):
        raise ValueError("CALDAV_PUBLIC_URL must be another calendar than CALDAV_URL")
    return public
