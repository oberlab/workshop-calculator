"""Demo data (WM-160): an invented makerspace for screenshots, trials and the public repository.

`generate` fills an empty database with made-up but plausible data for every table the app uses:
places in the example instance's map municipalities (Musterstadt, Beispieldorf, Neudorf) and one
outside, contexts (one project page that asks for the participants' home municipalities), formats
(one continuous, several with equipment needs), the team with weekly hours and fake Slack IDs,
contacts (example.org addresses, 0123 numbers), and courses with Termine over about two school years
up to a few weeks ahead in every stage: held with participants and Herkunft, planned, cancelled with
a reason, series. On top come a few deliberate cases so that every page has something to show: a
day on which the Calliope kits run short, a week in which the demo person is above their hours, a
double-booked person, a planned Termin in the past, Termine waiting for participants (the phone
form), proposed list entries, an open correction and an accepted hint. Calendar fields stay empty.

Nothing is real: no name of a place, person or organisation and no data is read from an instance or
from old files. The data is built around `today` and is the same for the same seed and day. The demo
person "Kim Beispiel" is linked to the login of `AUTH_MODE=off` (subject "off"), so Mein Bereich
shows their tasks without a login server.

The generator only writes to the database it is given and refuses one that has data already
(`NotEmpty`); `reset` empties a database it filled before (`ImportRun` of kind "demo", no imported
Termine), for throwaway demo databases only.
"""

import datetime as dt
import random
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from metrics import (
    hints,
    lists,
    logins,  # noqa: F401  (registers login_sessions, a table of the schema too)
)
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    BESTAETIGT,
    CONFIRMED,
    DURCHGEFUEHRT,
    GEPLANT,
    AlternativeDate,
    Appointment,
    AppointmentEquipment,
    AppointmentOrigin,
    Base,
    Contact,
    Context,
    Course,
    Equipment,
    Format,
    FormatEquipment,
    HistoryEntry,
    ImportRun,
    Person,
    Place,
    SchoolType,
)
from metrics.ingest.records import TEACHERS, parse_class_age
from metrics.instance import Instance
from metrics.metrics import school_year_start
from metrics.planning import corrections

KIND = "demo"  # ImportRun.kind of a demo database
LOGIN_SUBJECT = "off"  # the login of AUTH_MODE=off
SOURCE = "app"  # demo Termine behave like Termine made in the app

# Tables the generator fills, and the ones it leaves empty on purpose (a test checks that every table
# of the schema is in one of the two).
FILLED = (
    "school_types",
    "places",
    "contexts",
    "formats",
    "equipment",
    "format_equipment",
    "people",
    "contacts",
    "courses",
    "appointments",
    "appointment_support",
    "appointment_equipment",
    "appointment_alternatives",
    "appointment_origins",
    "history",
    "proposals",
    "hint_decisions",
    "hints",
    "import_runs",
)
LEFT_EMPTY = {
    "calendar_responses": "calendar fields stay empty (no calendar server)",
    "calendar_orphans": "calendar fields stay empty (no calendar server)",
    "slack_messages": "written by the scheduler when it sends a message",
    "job_runs": "written by the scheduler",
    "login_sessions": "written at login",
}

# --- The invented world ---------------------------------------------------------------------------

# Postcode of each municipality, for the addresses.
POSTCODES = {"Musterstadt": "12345", "Beispieldorf": "12346", "Neudorf": "12347", "Nachbarstadt": "12350"}
# name, school type (None: not a school), street, municipality, lat, lon. Coordinates lie inside the
# example map's municipalities (instance.example/geo), except for Nachbarstadt (outside the map).
# fmt: off
PLACES = (
    ("Grundschule am Lindenplatz", "GS", "Lindenplatz 2", "Musterstadt", 48.072, 11.081),
    ("Grundschule Musterstadt-Süd", "GS", "Schulweg 1", "Musterstadt", 48.031, 11.112),
    ("Mittelschule Musterstadt", "MS", "Schulweg 3", "Musterstadt", 48.061, 11.121),
    ("Realschule am Stadtpark", "RS", "Parkallee 10", "Musterstadt", 48.093, 11.152),
    ("Gymnasium Musterstadt", "GYM", "Am Gymnasium 1", "Musterstadt", 48.112, 11.064),
    ("Grundschule Beispieldorf", "GS", "Dorfstraße 4", "Beispieldorf", 48.051, 11.271),
    ("Mittelschule Beispieldorf", "MS", "Kirchweg 7", "Beispieldorf", 48.083, 11.334),
    ("Grundschule Neudorf", "GS", "Am Anger 3", "Neudorf", 48.201, 11.152),
    ("Gymnasium Nachbarstadt", "GYM", "Bahnhofstraße 20", "Nachbarstadt", 47.94, 11.55),
    ("Makerspace Beispieldorf", None, "Werkstattstraße 5", "Beispieldorf", 48.08, 11.30),
    ("Stadtbücherei Musterstadt", None, "Marktplatz 1", "Musterstadt", 48.075, 11.10),
    ("Jugendtreff Neudorf", None, "Bachgasse 2", "Neudorf", 48.19, 11.28),
    ("Online", None, None, None, None, None),  # deliberately no address
)
# fmt: on
TRAVEL_HOURS = {"Gymnasium Nachbarstadt": Decimal(1)}  # outside the district
MAKERSPACE = "Makerspace Beispieldorf"
HOLIDAY_PLACES = (MAKERSPACE, "Stadtbücherei Musterstadt", "Jugendtreff Neudorf")

SCHOOL = "Schulkooperation"
DIGITAL_DAY = "Digitaltag"
HOLIDAYS = "Ferienprogramm"
TEACHER_TRAINING = "Lehrkräftefortbildung"
PROJECT = "Projekt A"
CONTEXTS = (
    dict(name=SCHOOL),
    dict(name=DIGITAL_DAY),
    dict(name=HOLIDAYS),
    dict(name=TEACHER_TRAINING, teacher_training=True),
    dict(name=PROJECT, is_project=True, page_slug="projekt-a", page_title=PROJECT, asks_municipalities=True),
)

# name, quantity, note
EQUIPMENT = (
    ("Calliope mini", 30, "Klassensatz, Schrank 3"),
    ("Laptopkoffer", 3, "je 15 Laptops"),
    ("Lötstationen-Koffer", 2, None),
    ("3D-Drucker", 4, "zwei davon nur im Makerspace"),
)

# name, category, hours, overhead, continuous, needs {equipment: quantity}
CALLIOPE = "Calliope mini: Erste Schritte"
LAB = "Offene Werkstatt"
TRAINING = "Digitale Werkzeuge im Unterricht"
FORMATS = (
    ("Scratch-Einstieg", "Code", "3", None, False, {"Laptopkoffer": 1}),
    (CALLIOPE, "Code", "3", None, False, {"Calliope mini": 16}),
    ("Hackday", "Code", "6", "3", False, {"Laptopkoffer": 2}),
    (TRAINING, "Code", "3", None, False, {"Laptopkoffer": 1}),
    ("Löten: Blinkender Stern", "Engineering", "2.5", None, False, {"Lötstationen-Koffer": 1}),
    ("3D-Druck kennenlernen", "Engineering", "3", None, False, {"3D-Drucker": 2}),
    ("Roboter bauen", "Engineering", "4", None, False, {}),
    (LAB, "Engineering", "2", None, True, {"3D-Drucker": 1}),
    ("Experimente mit Strom", "Science", "2", None, False, {}),
    ("Mikroskopie", "Science", "2", None, False, {}),
)
SCHOOL_FORMATS = (
    "Scratch-Einstieg",
    CALLIOPE,
    "Löten: Blinkender Stern",
    "3D-Druck kennenlernen",
    "Roboter bauen",
    "Experimente mit Strom",
    "Mikroskopie",
)
SERIES_FORMATS = ("Roboter bauen", "Scratch-Einstieg", CALLIOPE, "Mikroskopie")
HOLIDAY_FORMATS = ("Löten: Blinkender Stern", "3D-Druck kennenlernen", "Experimente mit Strom", "Hackday")

# name, weekly hours, active, lead weight; the first is the demo login.
KIM = "Kim Beispiel"
LEA = "Lea Muster"
SAM = "Sam Demo"
PEOPLE = (
    (KIM, "20", True, 3),
    (LEA, "30", True, 3),
    (SAM, "25", True, 2),
    ("Jo Probe", "20", True, 2),
    ("Mira Testfeld", "15", True, 2),
    ("Ben Vorlage", None, True, 1),  # a helper without weekly hours: the bars show the hours alone
    ("Ute Ehemalig", None, False, 0),  # left the team: only in older Termine
)
FORMER = "Ute Ehemalig"

CONTACT_ROLES = ("Klassenleitung", "Sekretariat", "Schulleitung", "IT-Beauftragte")
CONTACT_NAMES = (
    "Frau Muster",
    "Herr Beispiel",
    "Frau Probe",
    "Herr Demo",
    "Frau Vorlage",
    "Herr Platzhalter",
)

CANCEL_REASONS = (
    "Schule hat abgesagt (Krankheitswelle)",
    "Zu wenige Anmeldungen",
    "Kursleitung krank, kein Ersatz",
    "Raum nicht verfügbar",
    "Unwetterwarnung, Ausflug abgesagt",
)

# Rough school holidays (month, day) to (month, day): no school courses, holiday programme instead.
SCHOOL_HOLIDAYS = (
    ((8, 1), (9, 12)),
    ((10, 27), (11, 2)),
    ((12, 22), (12, 31)),
    ((1, 1), (1, 6)),
    ((3, 30), (4, 10)),
    ((6, 9), (6, 20)),
)
HOLIDAY_PROGRAMME = (((8, 1), (9, 12)), ((3, 30), (4, 10)))

RECENT_DAYS = 21  # Termine of the last three weeks may still wait for their final confirmation
CONFIRMED_DAYS = 21  # Termine of the next three weeks are Bestätigt, later ones Geplant
AHEAD_WEEKS = 10


class NotEmpty(Exception):
    """The database has data already; the generator writes only into an empty one."""


@dataclass
class Result:
    counts: dict[str, int]
    hints: Counter = field(default_factory=Counter)


def content_rows(session: Session) -> dict[str, int]:
    """Rows per table the generator fills (empty database: all zero)."""
    return {
        name: session.execute(select(func.count()).select_from(Base.metadata.tables[name])).scalar_one()
        for name in FILLED
    }


def table_counts(session: Session) -> dict[str, int]:
    """Rows per table of the schema."""
    return {
        name: session.execute(select(func.count()).select_from(table)).scalar_one()
        for name, table in sorted(Base.metadata.tables.items())
    }


def reset(session: Session) -> None:
    """Empty a database this generator filled before (throwaway demo databases only)."""
    demo = session.scalar(select(func.count(ImportRun.id)).where(ImportRun.kind == KIND))
    other = session.scalar(select(func.count(ImportRun.id)).where(ImportRun.kind != KIND))
    imported = session.scalar(select(func.count(Appointment.id)).where(Appointment.source != SOURCE))
    if not demo or other or imported:
        raise NotEmpty(
            "Diese Datenbank wurde nicht von `metrics demo-data` gefüllt (oder enthält importierte Daten); "
            "--reset leert nur Demo-Datenbanken."
        )
    names = ", ".join(f'"{name}"' for name in sorted(Base.metadata.tables))
    session.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


# --- Building ------------------------------------------------------------------------------------


def _in(day: dt.date, ranges) -> bool:
    return any(dt.date(day.year, *a) <= day <= dt.date(day.year, *b) for a, b in ranges)


def _time(text: str) -> dt.time:
    return dt.time.fromisoformat(text)


def _end(start: dt.time, hours: Decimal) -> dt.time:
    moment = dt.datetime.combine(dt.date.min, start) + dt.timedelta(hours=float(hours))
    return moment.time()


def _hours(start: dt.time | None, end: dt.time | None) -> Decimal | None:
    if not (start and end):
        return None
    seconds = (dt.datetime.combine(dt.date.min, end) - dt.datetime.combine(dt.date.min, start)).seconds
    return Decimal(str(round(seconds / 3600, 2)))


def _at(day: dt.date, hour: int = 9) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour), tzinfo=hints.LOCAL)


class _Builder:
    def __init__(self, session: Session, today: dt.date, seed: int, instance: Instance):
        self.s = session
        self.today = today
        self.rng = random.Random(seed)
        self.instance = instance
        self.history: list[tuple] = []  # (row, table, field, old, new, by, when, reason)
        self.termine: list[Appointment] = []
        self.proposed: dict[str, Place] = {}  # proposed places, never picked at random
        self.busy: dict[dt.date, set[str]] = {}  # who works on a day: random picks avoid double bookings
        self.booked: Counter = Counter()  # Termine per day: random days prefer quiet ones
        self.used: dict[dt.date, set[str]] = {}  # places in use per day: random picks avoid them
        self.slots: dict[dt.date, list[tuple[dt.time, dt.time]]] = {}  # times in use per day

    # Lists ---------------------------------------------------------------------------------------

    def lists(self) -> None:
        types = self.instance.school_types
        self.s.add_all(
            SchoolType(code=code, label=label, position=i) for i, (code, label) in enumerate(types.items())
        )
        self.places = {}
        for name, school_type, street, municipality, lat, lon in PLACES:
            self.places[name] = Place(
                name=name,
                is_school=school_type is not None,
                school_type=school_type,
                address=f"{street}, {POSTCODES[municipality]} {municipality}" if street else "",
                municipality=municipality,
                lat=lat,
                lon=lon,
                travel_hours=TRAVEL_HOURS.get(name),
                status=CONFIRMED,
            )
        self.contexts = {c["name"]: Context(**c, status=CONFIRMED) for c in CONTEXTS}
        self.equipment = {
            name: Equipment(name=name, quantity=n, note=note, status=CONFIRMED) for name, n, note in EQUIPMENT
        }
        self.formats = {}
        for name, category, hours, overhead, continuous, needs in FORMATS:
            self.formats[name] = Format(
                name=name,
                category=category,
                duration_hours=Decimal(hours),
                overhead_hours=Decimal(overhead) if overhead else None,
                continuous=continuous,
                status=CONFIRMED,
                equipment=[
                    FormatEquipment(equipment=self.equipment[e], quantity=q) for e, q in needs.items()
                ],
            )
        self.people = {}
        for i, (name, hours, active, _) in enumerate(PEOPLE, start=1):
            local = name.lower().replace(" ", ".")
            self.people[name] = Person(
                name=name,
                email=f"{local}@example.org",
                slack_id=f"U0DEMO{i:04d}",
                kimai_user=local,
                weekly_hours=Decimal(hours) if hours else None,
                active=active,
                login_subject=LOGIN_SUBJECT if name == KIM else None,
                status=CONFIRMED,
            )
        self.leads = [name for name, _, active, weight in PEOPLE for _ in range(weight) if active]
        self.s.add_all([*self.places.values(), *self.contexts.values(), *self.formats.values()])
        self.s.add_all([*self.equipment.values(), *self.people.values()])
        self.s.flush()
        self.contacts: dict[str, list[Contact]] = {}
        for name, place in self.places.items():
            if not place.is_school:
                continue
            slug = name.lower().replace(" ", "-").replace("ü", "ue").replace("ß", "ss")
            rows = []
            for role in self.rng.sample(CONTACT_ROLES, self.rng.choice((1, 2))):
                rows.append(
                    Contact(
                        place=place,
                        name=self.rng.choice(CONTACT_NAMES),
                        role=role,
                        email=f"{role.lower().replace('-', '')}.{slug}@example.org",
                        phone=f"0123 {self.rng.randint(100000, 999999)}",
                    )
                )
            self.s.add_all(rows)
            self.contacts[name] = rows
        for row in [*self.places.values(), *self.contexts.values(), *self.formats.values()]:
            self._log(row, row.__tablename__, "status", None, CONFIRMED, KIM, None, "Demodaten angelegt")

    # Courses and Termine -------------------------------------------------------------------------

    def _log(self, row, table, field, old, new, by, when, reason=None) -> None:
        self.history.append((row, table, field, old, new, by, when, reason))

    def _class_age(self, place: Place, context: Context) -> str:
        if context.teacher_training:
            return TEACHERS
        if place.is_school:
            levels = {"GS": (1, 4), "MS": (5, 9), "RS": (5, 10), "GYM": (5, 12)}[place.school_type]
            return f"Kl. {self.rng.randint(*levels)}"
        return f"{self.rng.randint(8, 13)} Jahre"

    def course(self, fmt: str, context: str, place: str, class_age: str | None = None, note=None) -> Course:
        place_row = self.places.get(place) or self.proposed[place]
        context_row = self.contexts[context]
        text_value = class_age or self._class_age(place_row, context_row)
        parsed = parse_class_age(text_value)
        course = Course(
            format=self.formats[fmt],
            context=context_row,
            place=place_row,
            class_age_text=text_value,
            class_level=parsed.class_level,
            age=parsed.age,
            age_band=parsed.age_band,
            note=note,
        )
        course.target_group = lists.course_target_group(course)
        if self.contacts.get(place) and self.rng.random() < 0.5:
            course.contact = self.rng.choice(self.contacts[place])
        self.s.add(course)
        return course

    def _participants(self, course: Course) -> tuple[int, int, int, int]:
        if course.target_group == TEACHERS:
            total = self.rng.randint(6, 16)
        elif course.context.name == PROJECT:
            total = self.rng.randint(5, 12)
        elif course.place.is_school:
            total = self.rng.randint(14, 27)
        else:
            total = self.rng.randint(6, 14)
        female = sum(self.rng.random() < 0.45 for _ in range(total))
        diverse = 1 if total > 10 and self.rng.random() < 0.05 else 0
        unspecified = 1 if total - female - diverse > 3 and self.rng.random() < 0.1 else 0
        return female, total - female - diverse - unspecified, diverse, unspecified

    def _origins(self, total: int) -> dict[str, int]:
        weights = {"Beispieldorf": 5, "Musterstadt": 3, "Neudorf": 2, "Nachbarstadt": 1}
        picks = self.rng.choices(list(weights), list(weights.values()), k=total)
        return dict(sorted(Counter(picks).items()))

    def _stage(self, day: dt.date) -> str:
        if day < self.today - dt.timedelta(days=RECENT_DAYS):
            return ABGESAGT if self.rng.random() < 0.05 else ABGESCHLOSSEN
        if day < self.today:
            return self.rng.choice(
                (ABGESCHLOSSEN, ABGESCHLOSSEN, DURCHGEFUEHRT, DURCHGEFUEHRT, DURCHGEFUEHRT)
            )
        if day < self.today + dt.timedelta(days=CONFIRMED_DAYS):
            return ABGESAGT if self.rng.random() < 0.04 else BESTAETIGT
        return GEPLANT

    def termin(
        self,
        course: Course,
        day: dt.date,
        start: str | None,
        *,
        stage: str | None = None,
        lead: str | None = None,
        support: tuple[str, ...] | None = None,
        participants: bool | None = None,
        hours: Decimal | None = None,
    ) -> Appointment:
        """One Termin with what its stage needs; `participants=False` leaves them out (held Termine:
        the phone form's task)."""
        if stage is None:
            stage = self._stage(day)
            if stage == GEPLANT and self.rng.random() < 0.2:
                start = None  # Geplant needs no times yet
        fmt = course.format
        start_time = _time(start) if start else None
        end_time = _end(start_time, hours or fmt.duration_hours) if start_time else None
        if lead is not None:
            lead_row = self.people[lead]
        elif stage == GEPLANT and self.rng.random() < 0.2:
            lead_row = None  # not staffed yet: the "Ohne Team" lane
        elif day < self.today - dt.timedelta(days=400) and self.rng.random() < 0.15:
            lead_row = self.people[FORMER]
        else:
            lead_row = self.people[self._free([day], self._team(day, self.leads))]
        if support is not None:
            support_rows = [self.people[n] for n in support]
        elif lead_row and self.rng.random() < 0.3:
            others = [n for n, _, active, _ in PEOPLE if active and n != lead_row.name]
            support_rows = [self.people[self._free([day], self._team(day, others))]]
        else:
            support_rows = []
        self.busy.setdefault(day, set()).update(p.name for p in [lead_row, *support_rows] if p)
        self.booked[day] += 1
        self.used.setdefault(day, set()).add(course.place.name)
        if start_time:
            self.slots.setdefault(day, []).append((start_time, end_time))
        t = Appointment(
            course=course,
            date=day,
            start_time=start_time,
            end_time=end_time,
            duration_hours=_hours(start_time, end_time),
            stage=stage,
            lead=lead_row,
            support=support_rows,
            source=SOURCE,
            equipment=[
                AppointmentEquipment(equipment=n.equipment, quantity=n.quantity) for n in fmt.equipment
            ],
        )
        if participants is None:
            if stage == DURCHGEFUEHRT:
                participants = self.rng.random() < 0.6  # else still to be entered (phone form)
            else:
                participants = stage == ABGESCHLOSSEN or (stage == BESTAETIGT and self.rng.random() < 0.2)
        if participants:
            counts = self._participants(course)
            t.participants_female, t.participants_male, t.participants_diverse, t.participants_unspecified = (
                counts
            )
            t.participants_total = sum(counts)
            if course.context.asks_municipalities:
                t.origins = [
                    AppointmentOrigin(municipality=m, participants=n)
                    for m, n in self._origins(t.participants_total).items()
                ]
        if stage == ABGESAGT:
            t.cancel_reason = self.rng.choice(CANCEL_REASONS)
        if stage == ABGESCHLOSSEN:
            t.billed = day < self.today - dt.timedelta(days=45) or self.rng.random() < 0.3
        self.s.add(t)
        self.termine.append(t)
        self._stage_history(t)
        return t

    def _team(self, day: dt.date, names: list[str]) -> list[str]:
        """Who random Termine may pick: Kim's coming weeks are the deliberate ones of `showcases` and
        the project's."""
        return names if day < self.today else [n for n in names if n != KIM]

    def _free(self, days: list[dt.date], names: list[str]) -> str:
        """A random name among those not yet working on any of the days (any of them if nobody is)."""
        free = [n for n in names if not any(n in self.busy.get(d, ()) for d in days)]
        return self.rng.choice(free or names)

    def _day(self, week: dt.date) -> dt.date:
        """A random weekday of the week, among the ones with the fewest Termine so far."""
        days = [week + dt.timedelta(days=i) for i in range(5)]
        fewest = min(self.booked[d] for d in days)
        return self.rng.choice([d for d in days if self.booked[d] == fewest])

    def _when(self, day: dt.date, hour: int) -> dt.datetime:
        """A Verlauf time: that day and hour, but never after this morning."""
        return min(_at(day, hour), _at(self.today, 8))

    def _stage_history(self, t: Appointment) -> None:
        planner = t.lead.name if t.lead else LEA
        created = self._when(t.date - dt.timedelta(days=self.rng.randint(20, 60)), 9)
        self._log(t, "appointments", "stage", None, GEPLANT, planner, created, "Termin angelegt")
        path = {
            GEPLANT: [],
            BESTAETIGT: [BESTAETIGT],
            DURCHGEFUEHRT: [BESTAETIGT, DURCHGEFUEHRT],
            ABGESCHLOSSEN: [BESTAETIGT, DURCHGEFUEHRT, ABGESCHLOSSEN],
            ABGESAGT: [BESTAETIGT, ABGESAGT],
        }[t.stage]
        when = {
            BESTAETIGT: self._when(t.date - dt.timedelta(days=14), 9),
            DURCHGEFUEHRT: self._when(t.date + dt.timedelta(days=1), 2),  # the scheduler's night move
            ABGESCHLOSSEN: self._when(t.date + dt.timedelta(days=self.rng.randint(2, 10)), 11),
            ABGESAGT: self._when(t.date - dt.timedelta(days=self.rng.randint(1, 5)), 8),
        }
        old = GEPLANT
        for stage in path:
            by = "System (automatisch)" if stage == DURCHGEFUEHRT else planner
            reason = {ABGESCHLOSSEN: "final bestätigt", ABGESAGT: t.cancel_reason}.get(stage)
            self._log(t, "appointments", "stage", old, stage, by, when[stage], reason)
            old = stage

    def series(self, course: Course, days: list[dt.date], start: str, **kwargs) -> list[Appointment]:
        lead = kwargs.pop("lead", None) or self._free(days, self._team(days[0], self.leads))
        return [self.termin(course, day, start, lead=lead, **kwargs) for day in days]

    def school_year(self, first: dt.date, last: dt.date) -> None:
        """Ordinary weeks between two Mondays: single dates, series, trainings, holiday programme and the
        weekly open workshop of the project."""
        week = first
        project_course = None
        while week <= last:
            year_start = school_year_start(week, self.instance.school_year_start_month)
            if (
                project_course is None
                or project_course.note != f"Schuljahr {year_start.year}/{year_start.year + 1}"
            ):
                project_course = self.course(
                    LAB,
                    PROJECT,
                    MAKERSPACE,
                    "12 Jahre",
                    note=f"Schuljahr {year_start.year}/{year_start.year + 1}",
                )
            if _in(week, SCHOOL_HOLIDAYS) or _in(week + dt.timedelta(days=4), SCHOOL_HOLIDAYS):
                if _in(week, HOLIDAY_PROGRAMME) or _in(week + dt.timedelta(days=4), HOLIDAY_PROGRAMME):
                    for _ in range(self.rng.choice((1, 2))):
                        day = self._day(week)
                        course = self.course(
                            self.rng.choice(HOLIDAY_FORMATS), HOLIDAYS, self.rng.choice(HOLIDAY_PLACES)
                        )
                        self.termin(course, day, "10:00")
            else:
                self.termin(project_course, week + dt.timedelta(days=2), "16:00", lead=self._project_lead())
                self._school_week(week)
            week += dt.timedelta(days=7)

    def _project_lead(self) -> str:
        return KIM if self.rng.random() < 0.7 else self.rng.choice((LEA, "Mira Testfeld"))

    def _start(self, day: dt.date, hours: Decimal) -> str:
        """A random morning start that overlaps no other Termin of the day (any if none is free)."""
        options = ("08:00", "08:30", "09:45", "10:00", "11:30", "12:00")
        free = [
            o
            for o in options
            if all(
                not (_time(o) < end and start < _end(_time(o), hours))
                for start, end in self.slots.get(day, ())
            )
        ]
        return self.rng.choice(free or options)

    def _place(self, days: list[dt.date], names: list[str]) -> str:
        """A random place among those not in use on any of the days (any of them if none is free)."""
        free = [n for n in names if not any(n in self.used.get(d, ()) for d in days)]
        return self.rng.choice(free or names)

    def _school_week(self, week: dt.date) -> None:
        schools = [name for name, place in self.places.items() if place.is_school]
        for _ in range(self.rng.choice((1, 2, 2, 3, 3, 4))):
            day = self._day(week)
            context = DIGITAL_DAY if self.rng.random() < 0.15 else SCHOOL
            course = self.course(self.rng.choice(SCHOOL_FORMATS), context, self._place([day], schools))
            self.termin(course, day, self._start(day, course.format.duration_hours))
        if self.rng.random() < 0.12:
            weekday = self.rng.randint(0, 4)
            count = self.rng.randint(4, 6)
            days = [week + dt.timedelta(days=weekday + 7 * i) for i in range(count)]
            days = [d for d in days if not _in(d, SCHOOL_HOLIDAYS)]
            course = self.course(self.rng.choice(SERIES_FORMATS), SCHOOL, self._place(days, schools))
            self.series(course, days, "13:30", hours=Decimal("1.5"))
        if self.rng.random() < 0.15:
            day = week + self.rng.choice((dt.timedelta(days=1), dt.timedelta(days=3)))
            course = self.course(TRAINING, TEACHER_TRAINING, self._place([day], [*schools, "Online"]))
            self.termin(course, day, "14:30")

    # The cases every page should show -------------------------------------------------------------

    def showcases(self) -> None:
        today = self.today
        monday = today - dt.timedelta(days=today.weekday())
        # Kim's open tasks: held Termine without participants (one asks for Herkunft), one ready to close.
        lab = self.course(LAB, PROJECT, MAKERSPACE, "11 Jahre", note="Zusatztermin")
        self.termin(
            lab, today - dt.timedelta(days=2), "16:00", stage=DURCHGEFUEHRT, lead=KIM, participants=False
        )
        school = self.course(CALLIOPE, SCHOOL, "Grundschule am Lindenplatz", "Kl. 4")
        self.termin(
            school, today - dt.timedelta(days=4), "08:30", stage=DURCHGEFUEHRT, lead=KIM, participants=False
        )
        done = self.course("Experimente mit Strom", SCHOOL, "Grundschule Neudorf", "Kl. 3")
        self.termin(
            done, today - dt.timedelta(days=6), "10:00", stage=DURCHGEFUEHRT, lead=KIM, participants=True
        )
        # The Calliope kits run short: two classes on the same day need 16 each, there are 30.
        short_day = monday + dt.timedelta(days=8)
        for place, lead, start in (
            ("Grundschule Musterstadt-Süd", LEA, "08:00"),
            ("Grundschule Beispieldorf", SAM, "12:00"),
        ):
            course = self.course(CALLIOPE, SCHOOL, place, "Kl. 4")
            self.termin(course, short_day, start, stage=BESTAETIGT, lead=lead, support=())
        # Kim above their weekly hours.
        busy = monday + dt.timedelta(days=14)
        for offset, place in (
            (0, "Mittelschule Musterstadt"),
            (1, "Realschule am Stadtpark"),
            (3, "Gymnasium Musterstadt"),
        ):
            course = self.course("Roboter bauen", SCHOOL, place)
            self.termin(
                course, busy + dt.timedelta(days=offset), "08:00", stage=BESTAETIGT, lead=KIM, support=()
            )
        # Sam in two places at once.
        clash = monday + dt.timedelta(days=10)
        for place in ("Mittelschule Beispieldorf", "Stadtbücherei Musterstadt"):
            course = self.course(
                "Mikroskopie", SCHOOL if place != "Stadtbücherei Musterstadt" else HOLIDAYS, place
            )
            self.termin(course, clash, "09:00", stage=BESTAETIGT, lead=SAM, support=())
        # A school cancelled next week.
        off = self.course("Löten: Blinkender Stern", SCHOOL, "Mittelschule Musterstadt", "Kl. 7")
        self.termin(off, monday + dt.timedelta(days=9), "10:00", stage=ABGESAGT, lead=LEA)
        # A planned Termin whose day has passed.
        late = self.course("Scratch-Einstieg", SCHOOL, "Grundschule Beispieldorf", "Kl. 3")
        self.termin(late, today - dt.timedelta(days=5), "09:00", stage=GEPLANT, lead=LEA)
        # A planned Termin with alternative dates and a series still to be confirmed.
        alt = self.course("3D-Druck kennenlernen", SCHOOL, "Realschule am Stadtpark", "Kl. 8")
        t = self.termin(alt, monday + dt.timedelta(days=37), "08:00", stage=GEPLANT, lead=SAM)
        t.alternatives = [AlternativeDate(date=monday + dt.timedelta(days=d)) for d in (38, 44)]
        robots = self.course("Roboter bauen", SCHOOL, "Gymnasium Musterstadt", "Kl. 6", note="Roboter-AG")
        self.series(
            robots,
            [d for i in range(10) if not _in(d := monday + dt.timedelta(days=30 + 7 * i), SCHOOL_HOLIDAYS)][
                :6
            ],
            "13:30",
            stage=GEPLANT,
            lead="Jo Probe",
            hours=Decimal("1.5"),
        )
        # A big Digitaltag: unusually many participants, accepted below.
        self.crowd = self.course("Hackday", DIGITAL_DAY, "Gymnasium Musterstadt", "Kl. 9")
        crowd = self.termin(self.crowd, today - dt.timedelta(days=60), "08:00", stage=ABGESCHLOSSEN, lead=LEA)
        crowd.participants_female, crowd.participants_male = 31, 33
        crowd.participants_diverse, crowd.participants_unspecified = 1, 0
        crowd.participants_total = 65
        self.crowd_termin = crowd

    # Numbers, history and decisions ---------------------------------------------------------------

    def numbers(self) -> None:
        """The Terminnummer as `courses.renumber` computes it: position among the course's Termine
        that are not cancelled, by date and start time."""
        by_course: dict[int, list[Appointment]] = {}
        for t in self.termine:
            by_course.setdefault(id(t.course), []).append(t)
        for termine in by_course.values():
            position = 0
            for t in sorted(termine, key=lambda a: (a.date, a.start_time or dt.time.min)):
                if t.stage != ABGESAGT:
                    position += 1
                t.number = position if t.stage != ABGESAGT else None

    def write_history(self) -> None:
        self.s.flush()
        seen_courses = set()
        rows = []
        for row, table, field_name, old, new, by, when, reason in self.history:
            if isinstance(row, Appointment) and row.course_id not in seen_courses:
                seen_courses.add(row.course_id)
                rows.append(
                    HistoryEntry(
                        table_name="courses",
                        row_id=row.course_id,
                        changed_by=by,
                        changed_at=when,
                        reason="Kurs angelegt",
                    )
                )
            rows.append(
                HistoryEntry(
                    table_name=table,
                    row_id=row.id,
                    field=field_name,
                    old_value=old,
                    new_value=new,
                    changed_by=by,
                    changed_at=when or dt.datetime.combine(self.today, dt.time(8), tzinfo=hints.LOCAL),
                    reason=reason,
                )
            )
        self.s.add_all(rows)

    def proposed_place(self) -> None:
        """A place a team member proposed and already uses for a planned course."""
        place = lists.propose(
            self.s,
            lists.PLACES,
            {
                "name": "Grundschule Neudorf-West",
                "is_school": True,
                "school_type": "GS",
                "address": "Wiesenweg 8, 12347 Neudorf",
                "municipality": "Neudorf",
            },
            LEA,
            "Neue Anfrage einer 3. Klasse",
        ).entry
        self.proposed[place.name] = place
        monday = self.today - dt.timedelta(days=self.today.weekday())
        course = self.course("Experimente mit Strom", SCHOOL, place.name, "Kl. 3")
        self.termin(course, monday + dt.timedelta(days=29), "09:00", stage=GEPLANT, lead=LEA)

    def proposals(self) -> None:
        """Proposed list entries, decided ones and an open correction."""
        lists.propose(self.s, lists.EQUIPMENT, {"name": "VR-Brillen", "quantity": 8, "note": "Leihgabe"}, SAM)
        lists.propose(
            self.s, lists.PEOPLE, {"name": "Tom Aushilfe", "email": "tom.aushilfe@example.org"}, KIM
        )
        python = lists.propose(
            self.s,
            lists.FORMATS,
            {"name": "Programmieren mit Python", "category": "Code", "duration_hours": Decimal("3")},
            "Jo Probe",
        ).entry
        lists.confirm(self.s, lists.FORMATS, python, KIM, "Passt ins Angebot")
        duplicate = lists.propose(self.s, lists.CONTEXTS, {"name": "Schulkoop"}, "Mira Testfeld").entry
        lists.merge(self.s, lists.CONTEXTS, duplicate, self.contexts[SCHOOL], KIM, "Gleicher Kontext")
        # An open correction of a held Termin.
        target = max(
            (
                t
                for t in self.termine
                if t.stage == ABGESCHLOSSEN
                and t.course.context.name == SCHOOL
                and (t.participants_male or 0) > 1
            ),
            key=lambda t: (t.date, t.id),
        )
        form = {
            "participants_female": [str(target.participants_female + 1)],
            "participants_male": [str(target.participants_male - 1)],
        }
        corrections.propose(self.s, target, form, "Nachmeldung der Lehrkraft: ein Kind falsch gezählt", LEA)

    def decisions(self) -> Counter:
        found = hints.refresh_all(self.s, self.today)
        hints.accept(
            self.s, self.crowd_termin.id, "viele-teilnehmende", "Ganze Jahrgangsstufe in der Aula", LEA
        )
        return Counter(f.rule for f in found)


def generate(session: Session, today: dt.date, seed: int, instance: Instance) -> Result:
    """Fill an empty database with demo data around `today`; refuses a database with data."""
    if rows := {name: n for name, n in content_rows(session).items() if n}:
        raise NotEmpty(
            "Die Datenbank ist nicht leer ("
            + ", ".join(f"{name}: {n}" for name, n in rows.items())
            + "); Demodaten nur in eine leere Datenbank."
        )
    b = _Builder(session, today, seed, instance)
    b.lists()
    b.proposed_place()
    b.showcases()  # first, so that the random Termine keep clear of their people
    start = school_year_start(today, instance.school_year_start_month)
    first = start.replace(year=start.year - 2)  # two school years back
    first += dt.timedelta(days=(7 - first.weekday()) % 7)  # a Monday
    b.school_year(first, today + dt.timedelta(weeks=AHEAD_WEEKS))
    b.numbers()
    b.write_history()
    session.flush()
    b.proposals()
    session.flush()
    found = b.decisions()
    session.add(
        ImportRun(
            kind=KIND,
            finished_at=dt.datetime.now(dt.UTC),
            rows=len(b.termine),
            message=f"Demodaten (Startwert {seed}, Stichtag {today.isoformat()})",
        )
    )
    session.flush()
    return Result(table_counts(session), found)
