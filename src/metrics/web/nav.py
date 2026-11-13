"""The sidebar navigation (`PROPOSAL.md` section 5.1): one list of entries, filtered per login.

Every entry names its group, the role it needs and the sidebar counters it shows. Entries for pages
that do not exist yet carry the ticket that adds them in `planned` and stay hidden; adding the page
means deleting that argument. `PROJECTS` marks where the selected projects of the instance appear.
"""

from dataclasses import dataclass

PUBLIC = "public"
TEAM = "team"
ADMIN = "admin"

PLANUNG = "Planung"
AUSWERTUNG = "Auswertung"
STAMMDATEN = "Stammdaten"
VERWALTUNG = "Verwaltung"


@dataclass(frozen=True)
class Count:
    """A counter next to an entry: a key of `overview.sidebar_counts()` and how it looks."""

    key: str
    tone: str  # critical, warning or neutral
    title: str  # read as "<n> <title>"


@dataclass(frozen=True)
class Entry:
    label: str
    href: str
    group: str | None = None
    role: str = TEAM
    counts: tuple[Count, ...] = ()
    note: str | None = None  # a quiet label instead of a counter, e.g. "öffentlich"
    action: bool = False  # shown as an action ("+ Neuer Kurs")
    planned: str | None = None  # the ticket that adds the page; hidden until then


PROJECTS = "projects"


def _proposed(slug: str, label: str) -> Count:
    return Count(f"list_{slug}", "warning", f"{label} warten auf Bestätigung")


ENTRIES: list[Entry | str] = [
    Entry("Übersicht", "/", role=PUBLIC, note="öffentlich"),
    Entry("Mein Bereich", "/mein-bereich", planned="WM-112"),
    Entry("Kalender", "/kalender", PLANUNG, planned="WM-112"),
    Entry(
        "Kurse",
        "/planung",
        PLANUNG,
        counts=(
            Count("courses_high", "critical", "Kurse mit kritischem Hinweis"),
            Count("courses_medium", "warning", "Kurse mit wichtigem Hinweis"),
        ),
    ),
    Entry("Team", "/team", PLANUNG, planned="WM-131"),
    Entry("+ Neuer Kurs", "/planung/neu", PLANUNG, action=True),
    Entry("Analyse", "/analyse", AUSWERTUNG),
    PROJECTS,
    Entry("Tabelle", "/tabelle", AUSWERTUNG, planned="WM-152"),
    Entry("Orte", "/stammdaten/orte", STAMMDATEN, counts=(_proposed("orte", "Orte"),)),
    Entry("Kontexte", "/stammdaten/kontexte", STAMMDATEN, counts=(_proposed("kontexte", "Kontexte"),)),
    Entry("Formate", "/stammdaten/formate", STAMMDATEN, counts=(_proposed("formate", "Formate"),)),
    Entry(
        "Personen",
        "/stammdaten/personen",
        STAMMDATEN,
        role=ADMIN,
        counts=(_proposed("personen", "Personen"),),
    ),
    Entry(
        "Prüfen",
        "/pruefen",
        VERWALTUNG,
        role=ADMIN,
        counts=(
            Count("hints_high", "critical", "kritische Hinweise"),
            Count("pruefen_open", "neutral", "Vorschläge und wichtige Hinweise"),
        ),
    ),
    Entry("Review", "/review", VERWALTUNG, role=ADMIN, counts=(Count("review", "neutral", "offene Punkte"),)),
    Entry("Betrieb", "/betrieb", VERWALTUNG, role=ADMIN, planned="WM-103"),
]


@dataclass(frozen=True)
class Link:
    label: str
    href: str
    active: bool
    counts: list[tuple[int, str, str]]  # (n, tone, title), only counters above zero
    note: str | None
    action: bool


def _allowed(entry: Entry, login) -> bool:
    if entry.role == PUBLIC:
        return True
    return login is not None and (entry.role == TEAM or login.admin)


def _matches(href: str, path: str) -> bool:
    return path == href or (href != "/" and path.startswith(href + "/"))


def sidebar(login, path: str, counts: dict[str, int], projects) -> list[tuple[str | None, list[Link]]]:
    """The visible entries in groups, in order; the longest matching address is the active one."""
    entries: list[Entry] = []
    for entry in ENTRIES:
        if entry == PROJECTS:
            if login is not None:
                entries += [Entry(p.title, f"/projekte/{p.slug}", AUSWERTUNG) for p in projects]
        elif not entry.planned and _allowed(entry, login):
            entries.append(entry)
    matching = [e.href for e in entries if _matches(e.href, path)]
    active = max(matching, key=len, default=None)
    groups: list[tuple[str | None, list[Link]]] = []
    for entry in entries:
        link = Link(
            entry.label,
            entry.href,
            entry.href == active,
            [(counts[c.key], c.tone, c.title) for c in entry.counts if counts.get(c.key)],
            entry.note if login is not None else None,
            entry.action,
        )
        if groups and groups[-1][0] == entry.group:
            groups[-1][1].append(link)
        else:
            groups.append((entry.group, [link]))
    return groups
