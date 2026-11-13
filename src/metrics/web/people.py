"""The Personen page (admins only, WM-105): the list page of `web/lists.py` for the team.

Admins keep every detail of a person by hand (name, e-mail, Slack ID, Kimai user, active, weekly
hours); the edit form checks the e-mail and the Slack ID for format and uniqueness and writes every
change to the Verlauf. The filters "ohne Slack-ID" and "ohne Anmeldung" show what is still missing;
neither blocks a confirmation. The login is linked at the first login by verified e-mail (WM-101).

Every route here is an admin route in `access.py`. The people picker of the course pages stays at
`/stammdaten/personen/auswahl`, open to the team (choose, or propose a new person).
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from metrics import lists
from metrics.db import MERGED
from metrics.web import lists as list_pages

router = APIRouter()
BASE = "/stammdaten/personen"

PERSON_FILTERS = {
    "ohne_slack": "ohne Slack-ID",
    "ohne_konto": "ohne Anmeldung",
}


def page_context(params, entries: list) -> dict:
    """The filters of the Personen page, with counts; narrows `entries` in place."""
    missing = {
        "ohne_slack": [p for p in entries if not p.slack_id and p.status != MERGED],
        "ohne_konto": [p for p in entries if not p.login_subject and p.status != MERGED],
    }
    chosen = [key for key in PERSON_FILTERS if params.get(key) == "1"]
    for key in chosen:
        entries[:] = [p for p in entries if p in missing[key]]
    return {
        "person_filters": PERSON_FILTERS,
        "person_filter_counts": {key: len(found) for key, found in missing.items()},
        "person_filters_on": chosen,
        # A note in the list, not a block: the Slack ID is wanted for messages (WM-130), not required.
        "problems": {p.id: ["ohne Slack-ID"] for p in missing["ohne_slack"] if p.active},
        "filter_query_people": "".join(f"&{key}=1" for key in chosen),
    }


@router.get(BASE, response_class=HTMLResponse)
def people_page(request: Request):
    return list_pages.show(request, lists.PEOPLE)


@router.post(BASE, response_class=HTMLResponse)
async def propose(request: Request):
    return await list_pages.propose_entry(request, lists.PEOPLE)


@router.post(BASE + "/{entry_id:int}", response_class=HTMLResponse)
async def edit(entry_id: int, request: Request):
    return await list_pages.edit_entry(request, lists.PEOPLE, entry_id)
