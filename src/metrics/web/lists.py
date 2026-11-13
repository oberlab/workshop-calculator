"""Stammdaten: one list-and-detail page per list and the picker for forms (WM-101).

`/stammdaten/<liste>` (orte, kontexte, formate, personen) shows the entries with search and a status
filter; `?id=` opens an entry in the detail panel (details, edit form, admin decisions, Verlauf),
`?neu=1` the form for a new proposal. The admin decisions are separate POST routes, listed as admin
routes in `access.py`. The Personen page is admin-only and has its own routes (`web/people.py`, which
reuses the handlers here); the generic list routes refuse it, while the people picker stays open to
the team.

The picker (`_lists.html`, macro `picker`) is a select of the active entries with a "Neu vorschlagen"
button. `GET /stammdaten/<liste>/auswahl` renders it (htmx swaps it in place: after a change, to show
the details of the chosen entry, or with the proposal fields open); `POST` to the same address adds
the proposed entry and answers with the picker that has it selected. Parameters: `feld` (the form
field, e.g. `place_id`; its value is the entry ID), `label`, `neu=1`, `ohne=<id>` (leave one entry
out, e.g. the entry being merged) and `neu_erlaubt=0` (no proposals, e.g. a merge target).
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from metrics import hints, lists
from metrics.db import CONFIRMED, MERGED, PROPOSED, get_sessionmaker
from metrics.instance import get_instance
from metrics.web import sessions
from metrics.web.common import TIMEZONE, current_login, page, templates

router = APIRouter()

STATUS_FILTERS = {
    "aktiv": "Vorgeschlagen und bestätigt",
    "vorgeschlagen": "Vorgeschlagen",
    "bestätigt": "Bestätigt",
    "abgelehnt": "Abgelehnt",
    "zusammengeführt": "Zusammengeführt",
    "alle": "Alle",
}
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,39}")
FIELD_LABELS = {
    "status": "Status",
    "merged_into_id": "Zusammengeführt in",
    "login_subject": "Anmeldung",
} | {f.name: f.label for fields in lists.FIELDS.values() for f in fields}


def _kind(slug: str, people: bool = True) -> lists.Kind:
    """The list of an address part; `people=False` for the routes the Personen page replaces."""
    kind = lists.KINDS.get(slug)
    if kind is None or (kind is lists.PEOPLE and not people):
        raise HTTPException(404, "Unbekannte Liste")
    return kind


async def _form(request: Request) -> dict[str, str]:
    form = parse_qs((await request.body()).decode(errors="replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in form.items()}


def _refresh_hints(session, kind: lists.Kind, *entry_ids: int) -> None:
    """The hints of every Termin using these entries follow a decision or an edit (WM-113)."""
    hints.refresh(session, hints.appointments_using(session, kind, entry_ids))


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _who(request: Request) -> str:
    return current_login(request).user


def _back(kind: lists.Kind, entry_id: int | None, message: str) -> RedirectResponse:
    target = f"/stammdaten/{kind.slug}?hinweis={quote(message)}"
    if entry_id is not None:
        target += f"&id={entry_id}#eintrag"
    return RedirectResponse(target, status_code=303)


def _fields(kind: lists.Kind, new: bool) -> list[lists.Field]:
    names = lists.PROPOSAL_FIELDS[kind] if new else [f.name for f in lists.FIELDS[kind]]
    return [f for f in lists.FIELDS[kind] if f.name in names]


@dataclass
class Detail:
    """The open entry of the list page."""

    entry: object
    values: dict[str, str]
    errors: dict[str, str]
    editable: bool
    usage: lists.Usage
    history: list
    proposal: object | None
    merged_into: object | None
    problems: list[str]
    missing: list[str]


def _list_page(
    request: Request,
    kind: lists.Kind,
    *,
    detail_values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    new_values: dict[str, str] | None = None,
    status_code: int = 200,
    error: str | None = None,
    selected_id: int | None = None,
):
    params = request.query_params
    query = (params.get("q") or "").strip()[:100]
    status = params.get("status") or "aktiv"
    if status not in STATUS_FILTERS:
        status = "aktiv"
    gaps = kind is lists.PLACES and params.get("luecken") == "1"
    selected_id = selected_id or _int(params.get("id"))
    login = current_login(request)
    instance = get_instance()
    with get_sessionmaker()() as session:
        types = lists.school_types(session)
        entries = lists.search(session, kind, query, None if status == "alle" else status)
        problems = {}
        if kind is lists.PLACES:
            problems = {p.id: lists.place_problems(p, types, instance.map) for p in entries}
            if gaps:
                entries = [p for p in entries if problems[p.id]]
        used = lists.usage(session, kind)
        detail = None
        entry = lists.get(session, kind, selected_id) if selected_id else None
        if entry is not None:
            detail = Detail(
                entry=entry,
                values=detail_values if detail_values is not None else lists.form_values(kind, entry),
                errors=errors or {},
                editable=lists.may_edit(entry, login.admin),
                usage=used.get(entry.id, lists.Usage()),
                history=lists.history(session, kind, entry.id),
                proposal=lists.open_proposal(session, entry),
                merged_into=lists.resolve(session, entry) if entry.status == MERGED else None,
                problems=lists.place_problems(entry, types, instance.map) if kind is lists.PLACES else [],
                missing=lists.missing_details(kind, entry),
            )
        status_counts = lists.counts_by_status(session, kind)
        proposed = lists.proposed_counts(session)
        merge_picker = None
        if detail is not None:
            merge_picker = picker_context(
                kind,
                "ziel",
                [e for e in lists.choices(session, kind) if e.id != entry.id],
                None,
                types,
                label="Zusammenführen in",
                allow_new=False,
                without=entry.id,
            )
        filter_query = urlencode(
            [("q", query)] * bool(query)
            + [("status", status)] * (status != "aktiv")
            + [("luecken", "1")] * gaps
        )
        context = {
            "kind": kind,
            "tabs": [
                (k.slug, k.label, f"/stammdaten/{k.slug}", proposed[f"list_{k.slug}"] or None)
                for k in lists.KINDS.values()
                if k is not lists.PEOPLE or login.admin
            ],
            "filter_counts": {
                **{key: status_counts.get(key, 0) for key in STATUS_FILTERS},
                "aktiv": status_counts[PROPOSED] + status_counts[CONFIRMED],
                "alle": sum(status_counts.values()),
            },
            "filter_query": "&" + filter_query if filter_query else "",
            "merge_picker": merge_picker,
            "entries": entries,
            "describe": {e.id: lists.describe(e, types) for e in entries},
            "labels": {e.id: lists.label(e) for e in entries},
            "usage": used,
            "problems": problems,
            "query": query,
            "status": status,
            "status_filters": STATUS_FILTERS,
            "status_labels": lists.STATUS_LABELS,
            "gaps": gaps,
            "detail": detail,
            "detail_label": lists.label(detail.entry) if detail else None,
            "merged_label": lists.label(detail.merged_into) if detail and detail.merged_into else None,
            "new": params.get("neu") == "1" or new_values is not None,
            "new_values": new_values or {},
            "new_errors": errors if new_values is not None else {},
            "fields_new": _fields(kind, True),
            "fields_edit": _fields(kind, False),
            "school_types": types,
            "categories": lists.categories(session) if kind is lists.FORMATS else [],
            "field_labels": FIELD_LABELS,
            "admin": login.admin,
            # Admins see who is logged in on the Personen page (moved from the review page, WM-110).
            "logged_in": sessions.logged_in_people() if kind is lists.PEOPLE and login.admin else None,
            "message": params.get("hinweis"),
            "error": error,
            "timezone": TIMEZONE,
            "PROPOSED": PROPOSED,
            "MERGED": MERGED,
        }
        if kind is lists.PEOPLE:
            from metrics.web import people  # the people router imports this module

            context |= people.page_context(params, entries)
            context["filter_query"] += context.pop("filter_query_people")
        return page(request, "stammdaten.html", context, status_code=status_code)


def show(request: Request, kind: lists.Kind):
    return _list_page(request, kind)


@router.get("/stammdaten/{kind}", response_class=HTMLResponse)
def list_page(kind: str, request: Request):
    return show(request, _kind(kind, people=False))


@router.post("/stammdaten/{kind}", response_class=HTMLResponse)
async def propose(kind: str, request: Request):
    """A new proposed entry from the list page."""
    return await propose_entry(request, _kind(kind, people=False))


async def propose_entry(request: Request, list_kind: lists.Kind):
    form = await _form(request)
    with get_sessionmaker()() as session, session.begin():
        values, errors = lists.parse(list_kind, form, lists.school_types(session), new=True)
        if not errors:
            try:
                result = lists.propose(session, list_kind, values, _who(request), form.get("reason") or None)
            except lists.ListError as error:
                errors = {"name": str(error)}
    if errors:
        return _list_page(request, list_kind, new_values=form, errors=errors, status_code=422)
    # No names in the address: the message says only what happened.
    message = (
        "Vorgeschlagen: sofort nutzbar, wartet auf Bestätigung durch einen Admin."
        if result.created
        else "Gibt es schon: der vorhandene Eintrag ist geöffnet."
    )
    return _back(list_kind, result.entry.id, message)


@router.post("/stammdaten/{kind}/{entry_id:int}", response_class=HTMLResponse)
async def edit(kind: str, entry_id: int, request: Request):
    return await edit_entry(request, _kind(kind, people=False), entry_id)


async def edit_entry(request: Request, list_kind: lists.Kind, entry_id: int):
    form = await _form(request)
    login = current_login(request)
    with get_sessionmaker()() as session, session.begin():
        entry = _entry(session, list_kind, entry_id)
        values, errors = lists.parse(list_kind, form, lists.school_types(session), new=False)
        if list_kind is lists.PEOPLE and not errors:  # name the field that clashes (WM-105)
            errors = lists.person_conflicts(session, values, exclude_id=entry.id)
        changed = []
        if not errors:
            try:
                changed = lists.update(session, list_kind, entry, values, login.user, login.admin)
                if changed:
                    _refresh_hints(session, list_kind, entry.id)
            except lists.ListError as error:
                errors = {"name": str(error)}
    if errors:
        return _list_page(
            request, list_kind, detail_values=form, errors=errors, status_code=422, selected_id=entry_id
        )
    return _back(list_kind, entry_id, "Gespeichert." if changed else "Nichts geändert.")


def _entry(session, kind: lists.Kind, entry_id: int):
    entry = lists.get(session, kind, entry_id)
    if entry is None:
        raise HTTPException(404, "Unbekannter Eintrag")
    return entry


def _decision(request: Request, kind: lists.Kind, entry_id: int, error: lists.ListError):
    return _list_page(request, kind, status_code=422, error=str(error), selected_id=entry_id)


@router.post("/stammdaten/{kind}/{entry_id:int}/bestaetigen", response_class=HTMLResponse)
async def confirm(kind: str, entry_id: int, request: Request):
    list_kind = _kind(kind)
    form = await _form(request)
    try:
        with get_sessionmaker()() as session, session.begin():
            entry = _entry(session, list_kind, entry_id)
            lists.confirm(session, list_kind, entry, _who(request), form.get("reason") or None)
            _refresh_hints(session, list_kind, entry.id)
    except lists.ListError as error:
        return _decision(request, list_kind, entry_id, error)
    return _back(list_kind, entry_id, "Bestätigt.")


@router.post("/stammdaten/{kind}/{entry_id:int}/zusammenfuehren", response_class=HTMLResponse)
async def merge(kind: str, entry_id: int, request: Request):
    list_kind = _kind(kind)
    form = await _form(request)
    try:
        with get_sessionmaker()() as session, session.begin():
            entry = _entry(session, list_kind, entry_id)
            target_id = _int(form.get("ziel"))
            target = lists.get(session, list_kind, target_id) if target_id else None
            moved = lists.merge(session, list_kind, entry, target, _who(request), form.get("reason"))
            _refresh_hints(session, list_kind, entry.id, target.id)
    except lists.ListError as error:
        return _decision(request, list_kind, entry_id, error)
    what = "Termine" if list_kind is lists.PEOPLE else "Kurse"
    return _back(list_kind, target_id, f"Zusammengeführt; {moved} {what} übernommen.")


@router.post("/stammdaten/{kind}/{entry_id:int}/ablehnen", response_class=HTMLResponse)
async def reject(kind: str, entry_id: int, request: Request):
    list_kind = _kind(kind)
    form = await _form(request)
    try:
        with get_sessionmaker()() as session, session.begin():
            entry = _entry(session, list_kind, entry_id)
            in_use = lists.reject(session, list_kind, entry, _who(request), form.get("reason") or "")
            _refresh_hints(session, list_kind, entry.id)
    except lists.ListError as error:
        return _decision(request, list_kind, entry_id, error)
    message = "Abgelehnt."
    if in_use:
        message += f" Noch in {in_use} Kurs(en) eingetragen: dort ersetzen, sonst kein Abschluss."
    return _back(list_kind, entry_id, message)


# --- Picker ----------------------------------------------------------------------------------


def _picker(
    request: Request,
    kind: lists.Kind,
    *,
    selected_id: int | None,
    open_new: bool = False,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    message: str | None = None,
) -> HTMLResponse:
    params = request.query_params
    field = params.get("feld") or ""
    if not _FIELD.fullmatch(field):
        raise HTTPException(400, "Feld fehlt")
    without = _int(params.get("ohne"))
    allow_new = params.get("neu_erlaubt") != "0"
    with get_sessionmaker()() as session:
        types = lists.school_types(session)
        entries = [e for e in lists.choices(session, kind, selected_id) if e.id != without]
        p = picker_context(
            kind,
            field,
            entries,
            selected_id,
            types,
            label=(params.get("label") or kind.singular)[:60],
            allow_new=allow_new,
            without=without,
        )
        context = {
            "p": p,
            "open_new": open_new and allow_new,
            "new_values": values or {},
            "new_errors": errors or {},
            "message": message,
            "categories": lists.categories(session) if kind is lists.FORMATS else [],
        }
    return templates.TemplateResponse(request, "_picker.html", context)


def picker_context(
    kind: lists.Kind,
    field: str,
    entries: list,
    selected_id: int | None,
    types: dict[str, str],
    *,
    label: str | None = None,
    allow_new: bool = True,
    without: int | None = None,
) -> dict:
    """What the `picker` macro needs; page routes call this to render a picker inside their form."""
    selected = next((e for e in entries if e.id == selected_id), None)
    return {
        "kind": kind,
        "field": field,
        "label": label or kind.singular,
        "options": [
            (e.id, lists.label(e) + ("" if e.status == CONFIRMED else f" ({e.status})"), e.status)
            for e in entries
        ],
        "selected_id": selected.id if selected else None,
        "selected_note": lists.describe(selected, types) if selected else "",
        "allow_new": allow_new,
        "without": without,
        "fields_new": _fields(kind, True),
        "school_types": types,
    }


@router.get("/stammdaten/{kind}/auswahl", response_class=HTMLResponse)
def picker(kind: str, request: Request):
    params = request.query_params
    field = params.get("feld") or ""
    return _picker(
        request, _kind(kind), selected_id=_int(params.get(field)), open_new=params.get("neu") == "1"
    )


@router.post("/stammdaten/{kind}/auswahl", response_class=HTMLResponse)
async def picker_propose(kind: str, request: Request):
    """Propose a new entry from the picker; the answer is the picker with the new entry selected."""
    list_kind = _kind(kind)
    field = request.query_params.get("feld") or ""
    form = await _form(request)
    prefix = f"neu-{field}-"
    values_text = {key[len(prefix) :]: value for key, value in form.items() if key.startswith(prefix)}
    with get_sessionmaker()() as session, session.begin():
        values, errors = lists.parse(list_kind, values_text, lists.school_types(session), new=True)
        if not errors:
            try:
                result = lists.propose(session, list_kind, values, _who(request))
            except lists.ListError as error:
                errors = {"name": str(error)}
    if errors:
        return _picker(
            request,
            list_kind,
            selected_id=_int(form.get(field)),
            open_new=True,
            values=values_text,
            errors=errors,
        )
    message = (
        f"{list_kind.singular} vorgeschlagen: sofort nutzbar, wartet auf Bestätigung durch einen Admin."
        if result.created
        else result.note
    )
    return _picker(request, list_kind, selected_id=result.entry.id, message=message)
