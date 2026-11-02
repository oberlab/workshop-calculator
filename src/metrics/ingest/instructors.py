"""Names and profile pictures of instructors (Slack API users.info, scope `users:read`).

Every recorded instructor value is resolved to a person key through the instance aliases
(legacy first name -> Slack user ID). Slack users get their real name and profile picture;
people without a Slack user keep the recorded name and get no picture.
"""

import json
import re
import urllib.request

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from metrics.db import Instructor, SessionRow
from metrics.instance import Instance

SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{8,}$")


def _get_json(url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _download(url: str) -> tuple[bytes, str]:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read(), response.headers.get_content_type()


def person_keys(session: Session, instance: Instance) -> set[str]:
    recorded = session.execute(select(SessionRow.instructor).distinct()).scalars()
    return {key for value in recorded if (key := instance.instructor_key(value))}


def sync_instructors(session: Session, instance: Instance, token: str) -> tuple[int, int]:
    """Upsert one row per person. Returns (people, pictures)."""
    rows = []
    for key in sorted(person_keys(session, instance)):
        name, image, image_type = key, None, None
        if SLACK_USER_ID.match(key):
            result = _get_json(f"https://slack.com/api/users.info?user={key}", token)
            if not result.get("ok"):
                error = f"{result.get('error')} {result.get('needed', '')}"
                raise RuntimeError(f"users.info failed for {key}: {error}")
            profile = result["user"].get("profile", {})
            name = profile.get("real_name") or result["user"].get("real_name") or key
            if url := profile.get("image_192"):
                image, image_type = _download(url)
        rows.append({"key": key, "name": name, "image": image, "image_type": image_type})
    if rows:
        statement = insert(Instructor).values(rows)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["key"],
                set_={c: statement.excluded[c] for c in ("name", "image", "image_type", "fetched_at")},
            )
        )
    return len(rows), sum(1 for r in rows if r["image"])
