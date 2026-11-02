"""Internal ranking: held sessions per instructor (feature `instructor_ranking`)."""

from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import quote

from metrics.instance import Instance

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


def instructor_ranking(
    counts: list[tuple[str | None, int]], instance: Instance, people: dict[str, tuple[str, bool]]
) -> list[RankingRow]:
    """counts: (recorded instructor, sessions); people: key -> (name, has picture)."""
    per_person: dict[str | None, int] = defaultdict(int)
    for recorded, count in counts:
        per_person[instance.instructor_key(recorded)] += count
    rows = []
    for key, count in per_person.items():
        if key is None:
            rows.append(RankingRow(None, NO_INSTRUCTOR, count, None, "–"))
            continue
        name, has_image = people.get(key, (key, False))
        image_url = f"/instructors/{quote(key, safe='')}/image" if has_image else None
        rows.append(RankingRow(key, name, count, image_url, initials(name)))
    # Highest count first; sessions without instructor always last.
    return sorted(rows, key=lambda r: (r.key is None, -r.count, r.name.casefold()))
