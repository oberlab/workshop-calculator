"""Names and profile pictures of team members from Slack (API users.info, scope `users:read`).

People with a Slack user ID get their real name and profile picture. Legacy first names without
a Slack user (inactive people) keep the recorded name and get no picture.
"""

import json
import re
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from metrics.db import Person

SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{8,}$")


def _get_json(url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _download(url: str) -> tuple[bytes, str]:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read(), response.headers.get_content_type()


def sync_instructors(session: Session, token: str) -> tuple[int, int]:
    """Update every person with a Slack user ID. Returns (people, pictures)."""
    people = list(session.scalars(select(Person).where(Person.slack_id.is_not(None)).order_by(Person.id)))
    for person in people:
        if not SLACK_USER_ID.match(person.slack_id):
            continue
        result = _get_json(f"https://slack.com/api/users.info?user={person.slack_id}", token)
        if not result.get("ok"):
            error = f"{result.get('error')} {result.get('needed', '')}"
            raise RuntimeError(f"users.info failed for {person.slack_id}: {error}")
        profile = result["user"].get("profile", {})
        person.name = profile.get("real_name") or result["user"].get("real_name") or person.slack_id
        if url := profile.get("image_192"):
            person.image, person.image_type = _download(url)
    return len(people), sum(1 for p in people if p.image)
