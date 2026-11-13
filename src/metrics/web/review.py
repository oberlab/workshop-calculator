"""The admin review page and "Überall abmelden"; its planning actions live in the planning router."""

from dataclasses import dataclass
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from metrics import logins
from metrics.web import planning
from metrics.web.common import TIMEZONE, current_login, page

router = APIRouter()


@dataclass(frozen=True)
class Person:
    """Everyone with an open session, for the "Angemeldet" section."""

    subject: str
    user_name: str
    admin: bool
    sessions: int
    last_seen: str


def _logged_in_people() -> list[Person] | None:
    try:
        active = logins.active()
    except SQLAlchemyError:
        return None  # shown as "not available"; the rest of the page still works
    people: dict[str, list[logins.Active]] = {}
    for session in active:  # newest first
        people.setdefault(session.subject, []).append(session)
    return [
        Person(
            subject,
            sessions[0].user_name,
            any(s.admin for s in sessions),
            len(sessions),
            sessions[0].last_seen_at.astimezone(TIMEZONE).strftime("%d.%m., %H:%M Uhr"),
        )
        for subject, sessions in people.items()
    ]


@router.get("/review", response_class=HTMLResponse)
def review(request: Request):
    return page(
        request, "review.html", {"planning": planning.review_context(request), "people": _logged_in_people()}
    )


@router.post("/review/abmelden")
async def sign_out_everywhere(request: Request):
    """End every session of one person at once (e.g. after leaving the team or the admin group)."""
    form = parse_qs((await request.body()).decode(errors="replace"))
    subject = (form.get("subject") or [""])[0]
    if not subject:
        raise HTTPException(400, "Person fehlt")
    ended = logins.end_all(subject, ended_by=current_login(request).user)
    message = f"{ended} Sitzung(en) beendet." if ended else "Keine offene Sitzung."
    return RedirectResponse(f"/review?hinweis={quote(message)}", status_code=303)
