"""The "Tabelle" (WM-152): the held Termine of a selection as rows with chosen columns, sums per group
and a CSV download, for the team only.

Which Termine appear and how participants count comes from `metrics.metrics`: the rows are the held
Termine (Einsätze) of the analysis filters (`select_held`), and "Teilnehmende (gezählt)" is the
module's per-Termin participant rule (`counted_participants`, the first-Termin rule), so the column
sum equals the KPI tile Teilnehmende for the same filters. "Teilnehmende (erfasst)" is the number
recorded on the Termin, shown for reference.

Contact persons (WM-141) and their data are never a column, and neither are free texts that could
hold personal data (course note, cancellation reason) or the address of a place. Team members (lead,
support) may appear.

The CSV is written for Excel with a German locale: UTF-8 with a byte order mark, semicolons between
cells, decimal commas, dates as DD.MM.YYYY. Text cells that a spreadsheet would read as a formula get
a leading apostrophe (`csv_safe`).
"""

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from metrics import analytics, metrics
from metrics.db import Appointment, Course, Format, Person, Place, appointment_support


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str = "text"  # text, number, decimal, date, time, flag


COLUMNS = [
    Column("datum", "Datum", "date"),
    Column("beginn", "Beginn", "time"),
    Column("ende", "Ende", "time"),
    Column("dauer", "Dauer (h)", "decimal"),
    Column("terminnummer", "Terminnummer", "number"),
    Column("kurs", "Kurs-Nr.", "number"),
    Column("format", "Format"),
    Column("kategorie", "Kurskategorie"),
    Column("kontext", "Kontext"),
    Column("zielgruppe", "Zielgruppe"),
    Column("klasse", "Klasse/Alter"),
    Column("ort", "Ort"),
    Column("schule", "Schule", "flag"),
    Column("schulart", "Schulart"),
    Column("gemeinde", "Gemeinde"),
    Column("leitung", "Kursleitung"),
    Column("unterstuetzung", "Unterstützung"),
    Column("tn_gezaehlt", "Teilnehmende (gezählt)", "number"),
    Column("tn_erfasst", "Teilnehmende (erfasst)", "number"),
    Column("weiblich", "weiblich", "number"),
    Column("maennlich", "männlich", "number"),
    Column("divers", "divers", "number"),
    Column("keine_angabe", "keine Angabe", "number"),
    Column("abgerechnet", "Abgerechnet", "flag"),
]
BY_KEY = {c.key: c for c in COLUMNS}
DEFAULT_COLUMNS = [
    "datum",
    "format",
    "kontext",
    "ort",
    "terminnummer",
    "tn_gezaehlt",
    "tn_erfasst",
    "leitung",
]

# Columns the rows can be grouped by; the group's label is the column's value.
GROUPS = {
    "ort": "Ort",
    "schulart": "Schulart",
    "gemeinde": "Gemeinde",
    "kontext": "Kontext",
    "format": "Format",
    "kategorie": "Kurskategorie",
    "zielgruppe": "Zielgruppe",
    "leitung": "Kursleitung",
    "schuljahr": "Schuljahr",
    "monat": "Monat",
}
NO_VALUE = "(ohne Angabe)"


@dataclass
class Choice:
    """What the table shows beyond the analysis filters (all from the query string)."""

    columns: list[str] = field(default_factory=lambda: list(DEFAULT_COLUMNS))
    sort: str = "datum"
    descending: bool = False
    group: str = ""  # a key of GROUPS, or "" for none
    schools_only: bool = False


def parse_choice(params) -> Choice:
    """The table options from the query string; unknown values fall back to the defaults, so an old
    bookmark still opens."""
    asked = set(params.getlist("spalte"))
    columns = [c.key for c in COLUMNS if c.key in asked] or list(DEFAULT_COLUMNS)
    sort = params.get("sortierung") or "datum"
    group = params.get("gruppe") or ""
    return Choice(
        columns=columns,
        sort=sort if sort in BY_KEY else "datum",
        descending=params.get("absteigend") == "1",
        group=group if group in GROUPS else "",
        schools_only=params.get("schulen") == "1",
    )


@dataclass(frozen=True)
class Row:
    id: int
    course_id: int
    values: dict  # column key -> Python value (None when empty)


def _selection(filters: metrics.Filters, choice: Choice, today: date, school_year_start_month: int, *cols):
    query, period = metrics.select_held(select(*cols), filters, today, school_year_start_month)
    if choice.schools_only:
        query = query.where(Place.is_school.is_(True))
    return query, period


def rows(
    session: Session, filters: metrics.Filters, choice: Choice, today: date, school_year_start_month: int
) -> tuple[list[Row], tuple[date | None, date | None]]:
    """The held Termine of the selection, sorted as chosen, and the resolved period."""
    lead = aliased(Person)
    query, period = _selection(
        filters,
        choice,
        today,
        school_year_start_month,
        Appointment,
        Course.class_age_text,
        metrics.CATEGORY,
        metrics.CONTEXT,
        metrics.TARGET_GROUP,
        metrics.PLACE,
        Place.is_school,
        Place.school_type,
        Place.municipality,
        Format.name,
        lead.name,
        metrics.counted_participants.label("counted"),
    )
    query = query.outerjoin(lead, Appointment.lead_id == lead.id)
    found = session.execute(query).all()

    # Support names per Termin, for the same selection (one query, no list of IDs).
    ids, _ = _selection(filters, choice, today, school_year_start_month, Appointment.id)
    support = defaultdict(list)
    for appointment_id, name in session.execute(
        select(appointment_support.c.appointment_id, Person.name)
        .join(Person, Person.id == appointment_support.c.person_id)
        .where(appointment_support.c.appointment_id.in_(ids.scalar_subquery()))
        .order_by(Person.name)
    ):
        support[appointment_id].append(name)
    school_types = analytics.school_type_labels(session)

    result = []
    for (
        t,
        class_age,
        category,
        context,
        target_group,
        place,
        is_school,
        school_type,
        municipality,
        *rest,
    ) in found:
        format_name, lead_name, counted = rest
        values = {
            "datum": t.date,
            "beginn": t.start_time,
            "ende": t.end_time,
            "dauer": t.duration_hours,
            "terminnummer": t.number,
            "kurs": t.course_id,
            "format": format_name,
            "kategorie": category,
            "kontext": context,
            "zielgruppe": target_group,
            "klasse": class_age,
            "ort": place,
            "schule": is_school,
            "schulart": school_types.get(school_type, school_type) if school_type else None,
            "gemeinde": municipality,
            "leitung": lead_name,
            "unterstuetzung": ", ".join(support[t.id]) or None,
            "tn_gezaehlt": int(counted or 0),
            "tn_erfasst": t.participants_total,
            "weiblich": t.participants_female,
            "maennlich": t.participants_male,
            "divers": t.participants_diverse,
            "keine_angabe": t.participants_unspecified,
            "abgerechnet": t.billed,
        }
        result.append(Row(t.id, t.course_id, values))

    # Empty values last in either direction; ties by date and ID.
    result.sort(key=lambda r: (r.values["datum"], r.id))
    present = [r for r in result if r.values[choice.sort] is not None]
    missing = [r for r in result if r.values[choice.sort] is None]
    present.sort(key=lambda r: _sort_key(r.values[choice.sort]), reverse=choice.descending)
    return present + missing, period


def _sort_key(value):
    return value.casefold() if isinstance(value, str) else value


# ---- Groups -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Group:
    label: str
    sessions: int  # Einsätze
    participants: int  # Teilnehmende (gezählt)
    recorded: int  # Teilnehmende (erfasst)
    hours: Decimal


def _group_value(row: Row, group: str, school_year_start_month: int) -> tuple:
    """(sort key, label) of a row's group; empty values sort last."""
    when = row.values["datum"]
    if group == "schuljahr":
        start = metrics.school_year_start(when, school_year_start_month)
        return (0, start), analytics.bin_label(start, "school_year")
    if group == "monat":
        start = when.replace(day=1)
        return (0, start), analytics.bin_label(start, "month")
    value = row.values[group]
    if value is None:
        return (1, ""), NO_VALUE
    return (0, value.casefold()), value


def groups(found: list[Row], group: str, school_year_start_month: int) -> list[Group]:
    """Sums per group, in the group's order (alphabetical, or by time for school year and month)."""
    sums: dict = {}
    for row in found:
        key, label = _group_value(row, group, school_year_start_month)
        sessions, participants, recorded, hours = sums.get((key, label), (0, 0, 0, Decimal(0)))
        sums[(key, label)] = (
            sessions + 1,
            participants + row.values["tn_gezaehlt"],
            recorded + (row.values["tn_erfasst"] or 0),
            hours + (row.values["dauer"] or 0),
        )
    return [Group(label, *values) for (_, label), values in sorted(sums.items(), key=lambda item: item[0][0])]


def total(found: list[Row]) -> Group:
    return Group(
        "Gesamt",
        len(found),
        sum(r.values["tn_gezaehlt"] for r in found),
        sum(r.values["tn_erfasst"] or 0 for r in found),
        sum((r.values["dauer"] or 0 for r in found), Decimal(0)),
    )


# ---- Formatting and CSV -------------------------------------------------------------------------


def _decimal(value: Decimal) -> str:
    return f"{value.normalize():f}".replace(".", ",")


def text(value, kind: str) -> str:
    """A cell as German text: dates DD.MM.YYYY, times HH:MM, decimal commas, ja/nein."""
    if value is None:
        return ""
    if kind == "date":
        return f"{value:%d.%m.%Y}"
    if kind == "time":
        return f"{value:%H:%M}"
    if kind == "decimal":
        return _decimal(Decimal(value))
    if kind == "flag":
        return "ja" if value else "nein"
    return str(value)


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(cell: str) -> str:
    """A text cell a spreadsheet will not run as a formula (OWASP "CSV injection")."""
    return "'" + cell if cell.startswith(FORMULA_START) else cell


def _csv(header: list[str], body: list[list[str]]) -> str:
    out = io.StringIO()
    out.write("﻿")  # byte order mark: Excel then reads UTF-8
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow([csv_safe(h) for h in header])
    writer.writerows(body)
    return out.getvalue()


def rows_csv(found: list[Row], columns: list[str]) -> str:
    """One line per Termin with the chosen columns. Only text cells can start like a formula; numbers,
    dates and flags are written by this module."""
    chosen = [BY_KEY[key] for key in columns]
    body = []
    for row in found:
        cells = []
        for column in chosen:
            cell = text(row.values[column.key], column.kind)
            cells.append(csv_safe(cell) if column.kind == "text" else cell)
        body.append(cells)
    return _csv([c.label for c in chosen], body)


GROUP_HEADER = ["Einsätze", "Teilnehmende (gezählt)", "Teilnehmende (erfasst)", "Stunden"]


def group_cells(group: Group) -> list[str]:
    return [str(group.sessions), str(group.participants), str(group.recorded), text(group.hours, "decimal")]


def groups_csv(summary: list[Group], group: str, overall: Group) -> str:
    """One line per group plus the total."""
    body = [[csv_safe(g.label), *group_cells(g)] for g in [*summary, overall]]
    return _csv([GROUPS[group], *GROUP_HEADER], body)
