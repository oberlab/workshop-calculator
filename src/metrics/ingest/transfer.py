"""One-time transfer of the Slack Lists into Termine (WM-120, PROPOSAL.md section 4.10).

1. `fetch_lists` reads the Einsatztracking List and, if the instance has one, a project's own
   List (e.g. JugendLab) with scope `lists:read`; `write_archive` saves both (raw JSON and a CSV
   of every column) before anything is written to the database. Nothing in Slack is changed.
2. `transfer` reads such an archive, so a run can be repeated offline from the same files, and
   creates one course with one Termin per item dated after the baseline: open items in their
   stage, held items as Durchgeführt (their final confirmation happens in the app). Items that
   are already in the database (same Slack item ID) are skipped, so a second run adds nothing.

Names are stored as recorded. A place, context, format or person that is not a list entry yet
becomes a proposed entry (status `vorgeschlagen`, with a Vorschlag for the admin queue, WM-101),
so nothing unconfirmed reaches the statistics. People are matched by their Slack user ID.
"""

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from metrics import lists as reference_lists
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    BESTAETIGT,
    DURCHGEFUEHRT,
    GEPLANT,
    PROPOSED,
    STAGES,
    Appointment,
    AppointmentOrigin,
    Context,
    Course,
    Format,
    HistoryEntry,
    ImportRun,
    Person,
    Place,
    Proposal,
)
from metrics.ingest.archive import UPDATED_COLUMN, read_raw, write_raw
from metrics.ingest.legacy import SLACK_ID_COLUMN
from metrics.ingest.records import SessionRecord, build_record, parse_class_age, to_decimal, to_int, to_text
from metrics.ingest.slack import SOURCE_SLACK, ListSnapshot, fetch_list
from metrics.ingest.store import _Lists, baseline_end
from metrics.instance import Instance, clean_name

MAIN = "einsatztracking"
PROJECT = "projekt"
MAIN_LABEL = "Einsatztracking-Liste"
CHANGED_BY = "metrics transfer-slack"
TRANSFERRED = "aus Slack übernommen"

# Status in the List -> stage; `[slack.stages]` in the instance config adds or overrides.
DEFAULT_STAGES = {
    "In Planung": GEPLANT,
    "Geplant": GEPLANT,
    "Bestätigt": BESTAETIGT,
    "Durchgeführt": DURCHGEFUEHRT,
    "Abgesagt": ABGESAGT,
}

# Reasons for skipping an item; the first two are expected in bulk and only counted.
SKIP_DONE = "bereits übernommen"
SKIP_BASELINE = "durchgeführt, im Basisbestand (Datum bis Stichtag)"
SKIP_BEFORE = "nicht durchgeführt, Datum bis Stichtag"
SKIP_HELD_NO_DATE = "durchgeführt, aber ohne Datum"
SKIP_UNKNOWN_STATUS = "unbekannter Status (in [slack.stages] zuordnen)"
SKIP_UNREADABLE = "unlesbarer Wert"
COUNTED_ONLY = (SKIP_DONE, SKIP_BASELINE)

TABLE_LABELS = {"places": "Orte", "contexts": "Kontexte", "formats": "Formate", "people": "Personen"}


class TransferError(RuntimeError):
    """The transfer cannot start (no baseline, a column is missing, a setting is wrong)."""


@dataclass(frozen=True)
class ListSpec:
    """How the items of one List become Termine."""

    kind: str
    label: str
    columns: dict[str, str]  # field -> column name; every column must exist
    defaults: dict[str, str] = field(default_factory=dict)  # field -> value, for fields without a column
    origins: dict[str, tuple[str, ...]] = field(default_factory=dict)  # municipality -> columns
    ignored: frozenset[str] = frozenset()


def list_specs(instance: Instance) -> dict[str, ListSpec]:
    specs = {
        MAIN: ListSpec(MAIN, MAIN_LABEL, dict(instance.slack_columns), ignored=instance.slack_ignored_columns)
    }
    if project := instance.slack_project_list:
        p = project
        specs[PROJECT] = ListSpec(PROJECT, p.label, p.columns, p.defaults, p.origins, p.ignored_columns)
    return specs


def stage_map(instance: Instance) -> dict[str, str]:
    stages = {**DEFAULT_STAGES, **instance.slack_stages}
    for status, stage in stages.items():
        if stage not in STAGES or stage == ABGESCHLOSSEN:
            raise TransferError(
                f"[slack.stages]: {status!r} -> {stage!r} is not a stage the transfer may set "
                f"({', '.join(s for s in STAGES if s != ABGESCHLOSSEN)})"
            )
    return stages


# --- Archive ------------------------------------------------------------------------------


def fetch_lists(token: str, list_ids: dict[str, str]) -> dict[str, tuple[str, ListSnapshot]]:
    """Read the Lists (kind -> List ID) from Slack; read only."""
    return {kind: (list_id, fetch_list(token, list_id)) for kind, list_id in list_ids.items()}


def _values(item: dict, column: dict) -> list[str]:
    """The values of one cell as text (select choices as labels, every user of a user cell)."""
    for f in item.get("fields", []):
        if f.get("column_id") != column["id"]:
            continue
        kind = column["type"]
        if kind == "select":
            labels = {c["value"]: c["label"] for c in (column.get("options") or {}).get("choices", [])}
            return [labels.get(v, v) for v in f.get("select") or []]
        if kind == "text":
            return [f["text"]] if f.get("text") else []
        if isinstance(f.get(kind), list):
            return [str(v) for v in f[kind] if v not in (None, "")]
        value = f.get("value")
        return [str(value)] if value not in (None, "") else []
    return []


def _cell_text(item: dict, column: dict) -> str | None:
    return ", ".join(_values(item, column)) or None


def write_archive(out_dir: Path, lists: dict[str, tuple[str, ListSnapshot]]) -> Path:
    """Save each List as `<kind>.json` (complete, as fetched) and `<kind>.csv` (every column, one
    row per item, plus the Slack item ID and its last-edit time) in a new folder under `out_dir`."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    folder, n = out_dir / f"slack-listen_{stamp}", 1
    while folder.exists():  # never overwrite an earlier archive
        n += 1
        folder = out_dir / f"slack-listen_{stamp}-{n}"
    folder.mkdir(parents=True)
    for kind, (list_id, snapshot) in lists.items():
        write_raw(folder / f"{kind}.json", list_id, stamp, snapshot)
        with (folder / f"{kind}.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([c["name"] for c in snapshot.schema] + [SLACK_ID_COLUMN, UPDATED_COLUMN])
            for item in snapshot.items:
                cells = [_cell_text(item, column) or "" for column in snapshot.schema]
                writer.writerow([*cells, item["id"], str(item.get("updated_timestamp", ""))])
    return folder


def read_archive(folder: Path, instance: Instance) -> dict[str, tuple[str, ListSnapshot]]:
    """The Lists of an archive written by `write_archive` (or raw JSON files named the same way)."""
    lists = {}
    for kind in list_specs(instance):
        path = folder / f"{kind}.json"
        if not path.exists():
            raise TransferError(f"{path} is missing")
        lists[kind] = read_raw(path)
    return lists


# --- Transfer -----------------------------------------------------------------------------


@dataclass
class TransferReport:
    cutoff: date
    read: dict[str, int] = field(default_factory=dict)  # list label -> items
    created: Counter = field(default_factory=Counter)  # (list label, stage) -> Termine
    proposed: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))  # table -> names
    skipped: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))  # reason -> items
    unmapped_columns: dict[str, list[str]] = field(default_factory=dict)
    origin_mismatches: list[str] = field(default_factory=list)
    possible_duplicates: list[str] = field(default_factory=list)
    warnings: Counter = field(default_factory=Counter)

    @property
    def total_created(self) -> int:
        return sum(self.created.values())


def _find_column(schema: list[dict], name: str) -> dict | None:
    return next((c for c in schema if c["name"] == name), None) or next(
        (c for c in schema if clean_name(c["name"]) == clean_name(name)), None
    )


def _resolve(
    spec: ListSpec, snapshot: ListSnapshot
) -> tuple[dict[str, dict], dict[str, list[dict]], list[str]]:
    """Columns per field and per municipality, and the names of the columns nobody maps."""
    missing = []
    columns = {}
    for key, name in spec.columns.items():
        if (column := _find_column(snapshot.schema, name)) is None:
            missing.append(f"{name!r} ({key})")
        else:
            columns[key] = column
    origins: dict[str, list[dict]] = {}
    for municipality, names in spec.origins.items():
        for name in names:
            if (column := _find_column(snapshot.schema, name)) is None:
                missing.append(f"{name!r} (Herkunft {municipality})")
            else:
                origins.setdefault(municipality, []).append(column)
    if missing:
        raise TransferError(f"{spec.label}: column(s) not found: {', '.join(missing)}")
    if "billed" in columns:
        # The Einsatztracking List has two "Abgerechnet" columns: a Termin is billed if either is
        # ticked (Alex, 2026-11-13).
        name = clean_name(columns["billed"]["name"])
        same = [c["id"] for c in snapshot.schema if clean_name(c["name"]) == name]
        columns["billed"] = {**columns["billed"], "fused": same}
    used = {c["id"] for c in columns.values()} | {c["id"] for cs in origins.values() for c in cs}
    used |= set(columns.get("billed", {}).get("fused", []))
    ignored = {clean_name(name) for name in spec.ignored}
    unmapped = sorted(
        c["name"] for c in snapshot.schema if c["id"] not in used and clean_name(c["name"]) not in ignored
    )
    return columns, origins, unmapped


def _parse_date(text: str) -> date:
    """A List date: ISO (date columns) or German `DD.MM.YY`/`DD.MM.YYYY` (dates kept in a text
    column, as in the JugendLab List's "Name"). Raises ValueError on anything else."""
    text = text.strip()
    if match := re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})", text):
        day, month, year = (int(g) for g in match.groups())
        return date(year + 2000 if year < 100 else year, month, day)
    return date.fromisoformat(text[:10])


@dataclass
class _Item:
    """One List item, read into the fields the transfer needs."""

    item_id: str
    record: SessionRecord
    status: str | None
    team: list[str]  # Slack user IDs, the lead first
    origins: dict[str, int]
    recorded: dict[str, str | None]
    billed: bool = False


def _read_item(
    spec: ListSpec, item: dict, columns: dict[str, dict], origins: dict[str, list[dict]], instance: Instance
) -> _Item:
    """Raises ValueError on a value that cannot be read."""
    recorded = {key: _cell_text(item, column) for key, column in columns.items()}

    def value(key: str) -> str | None:
        return to_text(recorded[key] if key in columns else spec.defaults.get(key))

    def users(key: str) -> list[str]:
        if key in columns:
            return [clean_name(v) for v in _values(item, columns[key]) if clean_name(v)]
        return [v] if (v := value(key)) else []

    team = list(dict.fromkeys(users("instructor") + users("support")))
    raw_date = value("date")
    class_age_text = value("class_age")
    record = build_record(
        instance,
        source=SOURCE_SLACK,
        source_key=f"{SOURCE_SLACK}:{item['id']}",
        session_date=_parse_date(raw_date) if raw_date else None,
        course_name=value("course_name"),
        duration_hours=to_decimal(value("duration_hours")),
        location=value("location"),
        school_type=None,
        class_age_text=class_age_text,
        class_age=parse_class_age(class_age_text),
        participants_female=to_int(value("participants_female")),
        participants_male=to_int(value("participants_male")),
        participants_total=None,
        category=value("category"),
        context=value("context"),
        session_number=to_int(value("session_number")),
        status=value("status"),
        instructor=team[0] if team else None,
    )
    counts = {m: sum(to_int(_cell_text(item, c)) or 0 for c in cs) for m, cs in origins.items()}
    billed_ids = set(columns.get("billed", {}).get("fused", []))
    billed = any(
        f.get("checkbox") is True for f in item.get("fields", []) if f.get("column_id") in billed_ids
    )
    if billed_ids:
        recorded["billed"] = "ja" if billed else None
    return _Item(
        item_id=item["id"],
        record=record,
        status=record.status,
        team=team,
        origins={m: n for m, n in counts.items() if n},
        recorded=recorded | {f"Herkunft {m}": str(n) for m, n in counts.items() if n},
        billed=billed,
    )


def _stage(spec: ListSpec, item: _Item, stages: dict[str, str], today: date) -> str | None:
    """The stage of an item, or None for a status nobody has mapped.

    A List without a status column (a project List) only records sessions: an item counts as
    held when its date has passed and participants are recorded, otherwise it stays Geplant
    (with a past date it gets the critical hint, and a person decides).
    """
    if "status" in spec.columns or "status" in spec.defaults:
        return stages.get(item.status) if item.status else GEPLANT
    record = item.record
    recorded_participants = record.participants_total is not None or bool(item.origins)
    held = record.date is not None and record.date < today and recorded_participants
    return DURCHGEFUEHRT if held else GEPLANT


class _ProposedLists(_Lists):
    """List entries by recorded name; a missing one is added as proposed (status vorgeschlagen)."""

    def __init__(self, session: Session, instance: Instance):
        super().__init__(session, instance)
        self.source = ""
        self.new: list[tuple[Place | Context | Format | Person, str]] = []  # entry, first item

    def _get(self, cache, key, make):
        if key not in cache:
            entry = make()
            entry.status = PROPOSED
            cache[key] = entry
            self.session.add(entry)
            self.new.append((entry, self.source))
        return cache[key]


def _entry_name(entry) -> str:
    if isinstance(entry, Format):
        return f"{entry.name or '(ohne Kursname)'} / {entry.category or '(ohne Kategorie)'}"
    if isinstance(entry, Person):
        return entry.slack_id or entry.name
    return entry.name


def _proposed_values(entry) -> dict:
    if isinstance(entry, Format):
        return {"name": entry.name, "category": entry.category}
    if isinstance(entry, Person):
        return {"name": entry.name, "slack_id": entry.slack_id}
    return {"name": entry.name}


def _source(spec: ListSpec, item_id: str) -> str:
    return f"{TRANSFERRED} ({spec.label}, Eintrag {item_id})"


def transfer(
    session: Session, lists: dict[str, tuple[str, ListSnapshot]], instance: Instance, today: date
) -> TransferReport:
    """Create the Termine of every List item after the baseline; see the module docstring."""
    cutoff = baseline_end(session)
    if cutoff is None:
        raise TransferError("No baseline is loaded: the transfer takes the items after it.")
    specs = list_specs(instance)
    stages = stage_map(instance)
    # The target-group rule reads the teacher contexts from the contexts table (WM-101).
    instance = reference_lists.with_contexts(session, instance)
    report = TransferReport(cutoff=cutoff)
    done = set(session.scalars(select(Appointment.source_key).where(Appointment.source == SOURCE_SLACK)))
    lists_ = _ProposedLists(session, instance)
    created: list[tuple[ListSpec, str, Appointment]] = []

    for kind, spec in specs.items():
        list_id, snapshot = lists[kind]
        columns, origin_columns, report.unmapped_columns[spec.label] = _resolve(spec, snapshot)
        report.read[spec.label] = len(snapshot.items)
        for raw in snapshot.items:
            label = f"{spec.label} {raw['id']}"
            if f"{SOURCE_SLACK}:{raw['id']}" in done:
                report.skipped[SKIP_DONE].append(label)
                continue
            try:
                item = _read_item(spec, raw, columns, origin_columns, instance)
            except ValueError as error:
                report.skipped[SKIP_UNREADABLE].append(f"{label}: {error}")
                continue
            record = item.record
            label = f"{label} ({record.date or 'ohne Datum'}, {record.course_name or record.context or '-'})"
            stage = _stage(spec, item, stages, today)
            if record.date is not None and record.date <= cutoff:
                report.skipped[SKIP_BASELINE if stage == DURCHGEFUEHRT else SKIP_BEFORE].append(label)
                continue
            if stage is None:
                report.skipped[SKIP_UNKNOWN_STATUS].append(f"{label}: {item.status!r}")
                continue
            if stage == DURCHGEFUEHRT and record.date is None:
                report.skipped[SKIP_HELD_NO_DATE].append(label)
                continue

            done.add(record.source_key)
            lists_.source = _source(spec, item.item_id)
            for warning in record.warnings:
                report.warnings[warning] += 1
            course = Course(
                format=lists_.format(record.course_name, record.category)
                if record.course_name or record.category
                else None,
                context=lists_.context(record.context),
                place=lists_.place(record.location),
                class_age_text=record.class_age_text,
                class_level=record.class_level,
                age=record.age,
                age_band=record.age_band,
                target_group=record.target_group,
            )
            team = list(dict.fromkeys(p for user in item.team if (p := lists_.person(user))))
            appointment = Appointment(
                course=course,
                number=record.session_number,
                date=record.date,
                duration_hours=record.duration_hours,
                stage=stage,
                lead=team[0] if team else None,
                support=team[1:],
                participants_female=record.participants_female,
                participants_male=record.participants_male,
                participants_total=record.participants_total,
                billed=item.billed,
                source=SOURCE_SLACK,
                source_key=record.source_key,
                source_data={
                    "list": spec.label,
                    "list_id": list_id,
                    "item_id": item.item_id,
                    "updated_timestamp": raw.get("updated_timestamp"),
                    "recorded": item.recorded,
                    "defaults": {k: v for k, v in spec.defaults.items() if k not in columns},
                },
                origins=[AppointmentOrigin(municipality=m, participants=n) for m, n in item.origins.items()],
            )
            session.add(appointment)
            created.append((spec, label, appointment))
            report.created[(spec.label, stage)] += 1
            origin_sum = sum(item.origins.values())
            if item.origins and origin_sum != (record.participants_total or 0):
                report.origin_mismatches.append(
                    f"{label}: Herkunft {origin_sum}, Teilnehmende {record.participants_total}"
                )
    session.flush()

    for spec, _, appointment in created:
        session.add(
            HistoryEntry(
                table_name=Appointment.__tablename__,
                row_id=appointment.id,
                field="source_key",
                new_value=appointment.source_key,
                changed_by=CHANGED_BY,
                reason=_source(spec, appointment.source_data["item_id"]),
            )
        )
    for entry, source in lists_.new:
        table = entry.__tablename__
        report.proposed[table].append(_entry_name(entry))
        session.add(
            Proposal(
                kind="eintrag",
                target_table=table,
                target_id=entry.id,
                proposed_values=_proposed_values(entry),
                reason=source,
                proposed_by=CHANGED_BY,
            )
        )
        session.add(
            HistoryEntry(
                table_name=table,
                row_id=entry.id,
                field="status",
                new_value=PROPOSED,
                changed_by=CHANGED_BY,
                reason=source,
            )
        )

    # The same session in both Lists would be counted twice: flag it, a person decides.
    by_day: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
    for spec, label, appointment in created:
        context = appointment.course.context.name if appointment.course.context else None
        if appointment.date and context:
            by_day[(appointment.date, context)].append((spec.kind, label))
    for (day, context), entries in sorted(by_day.items()):
        if len({kind for kind, _ in entries}) > 1:
            report.possible_duplicates.append(f"{day} {context}: " + "; ".join(label for _, label in entries))

    session.add(
        ImportRun(
            kind="slack-transfer",
            finished_at=datetime.now(UTC),
            rows=report.total_created,
            message=f"{report.total_created} Termine aus Slack übernommen (nach {cutoff})",
        )
    )
    session.flush()
    return report


def format_report(report: TransferReport) -> str:
    lines = [f"Stichtag: Termine nach dem {report.cutoff} (Ende des Basisbestands)", "", "Gelesen:"]
    lines += [f"  {label}: {count} Einträge" for label, count in report.read.items()]
    lines += ["", f"Neue Termine: {report.total_created}"]
    for label in report.read:
        per_stage = [f"{s} {report.created[(label, s)]}" for s in STAGES if report.created[(label, s)]]
        lines.append(f"  {label}: {', '.join(per_stage) or 'keine'}")
    total_proposed = sum(len(v) for v in report.proposed.values())
    lines += ["", f"Neue Listeneinträge (vorgeschlagen, warten auf Bestätigung): {total_proposed}"]
    for table, label in TABLE_LABELS.items():
        if names := report.proposed.get(table):
            lines.append(f"  {label} ({len(names)}): " + "; ".join(sorted(names)))
    skipped = sum(len(v) for v in report.skipped.values())
    lines += ["", f"Übersprungen: {skipped}"]
    for reason, items in report.skipped.items():
        lines.append(f"  {reason}: {len(items)}")
        if reason not in COUNTED_ONLY:
            lines += [f"    {item}" for item in items]
    sections = (
        ("Herkunft passt nicht zur Summe der Teilnehmenden", report.origin_mismatches),
        ("Mögliche Doppelungen (gleicher Tag und Kontext in beiden Listen)", report.possible_duplicates),
        ("Hinweise", [f"{w} ({n})" for w, n in sorted(report.warnings.items())]),
        (
            "Spalten ohne Zuordnung (nicht übernommen)",
            [f"{label}: {', '.join(cols)}" for label, cols in report.unmapped_columns.items() if cols],
        ),
    )
    for title, entries in sections:
        if entries:
            lines += ["", f"{title}:"] + [f"  {entry}" for entry in entries]
    if not report.total_created and report.skipped.get(SKIP_DONE):
        lines += ["", "Nichts Neues: alle Einträge nach dem Stichtag sind bereits übernommen."]
    return "\n".join(lines)
