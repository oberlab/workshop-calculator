"""Internal ranking: held sessions per instructor (feature `instructor_ranking`)."""

from dataclasses import dataclass
from urllib.parse import quote

from metrics.analytics import PersonCount

NO_INSTRUCTOR = "Ohne Kursleitung"


@dataclass
class RankingRow:
    key: str | None
    name: str
    count: int
    image_url: str | None
    initials: str


def initials(name: str) -> str:
    parts = [p for p in name.replace("-", " ").split() if p[:1].isalpha()]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def instructor_ranking(counts: list[PersonCount]) -> list[RankingRow]:
    """One row per lead (the person list already joins a legacy first name to its Slack user)."""
    rows = []
    for person in counts:
        if person.key is None:
            rows.append(RankingRow(None, NO_INSTRUCTOR, person.count, None, "–"))
            continue
        name = person.name or person.key
        image_url = f"/instructors/{quote(person.key, safe='')}/image" if person.has_image else None
        rows.append(RankingRow(person.key, name, person.count, image_url, initials(name)))
    # Highest count first; sessions without instructor always last.
    return sorted(rows, key=lambda r: (r.key is None, -r.count, r.name.casefold()))
