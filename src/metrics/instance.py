"""Organisation-specific configuration from the instance directory.

The instance directory is private (never in the public repository). Everything in
it is optional: without it the app runs with the neutral defaults in this package.

    instance/
    ├── config.toml          calendar, target groups, Slack column mapping
    ├── theme/tokens.toml    colours, fonts, category colours
    ├── theme/fonts/         font files referenced by tokens.toml
    ├── seeds/locations.csv  lookup per location: school?, school type, address, coordinates, municipality
    ├── seeds/school_types.csv  school type codes and labels, in chart order
    ├── geo/                 municipality boundaries for the map (GeoJSON, see [map])
    ├── seeds/instructors.csv  legacy first names -> Slack user IDs (same person)
    └── mappings/            name aliases, suggestions only (kontext, kontext_inferred, kursname, ort)
"""

import csv
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from metrics.config import get_settings
from metrics.geo import Municipality, load_municipalities

DEFAULT_THEME = Path(__file__).parent / "theme_default" / "tokens.toml"

DEFAULT_SLACK_COLUMNS = {
    "date": "Datum",
    "course_name": "Kursname",
    "duration_hours": "Dauer (h)",
    "location": "Ort",
    "class_age": "Klassenstufe/\nDurchschnittsalter",
    "participants_female": "TN :female_sign:",
    "participants_male": "TN :male_sign:",
    "category": "Kurskategorie",
    "context": "Kontext",
    "session_number": "Terminnummer",
    "status": "Status",
    "instructor": "Kursleiter",
}


def clean_name(value: str | None) -> str:
    """Collapse whitespace; used for every name lookup."""
    return re.sub(r"\s+", " ", value or "").strip()


@dataclass(frozen=True)
class Theme:
    heading_font: str
    body_font: str
    font_files: list[dict]
    colors: dict[str, str]
    bar_opacity: float
    category_order: list[str]
    category_colors: dict[str, str]
    palette: list[str]
    base_dir: Path
    map_gradient: list[str] = field(default_factory=lambda: ["#c6dbef", "#6baed6", "#2171b5", "#08306b"])

    def color_for(self, category: str, index: int) -> str:
        return self.category_colors.get(category) or self.palette[index % len(self.palette)]


@dataclass(frozen=True)
class NameMappings:
    context_aliases: dict[str, str] = field(default_factory=dict)
    context_from_course: dict[str, str] = field(default_factory=dict)
    course_aliases: dict[str, str] = field(default_factory=dict)
    location_aliases: dict[str, str] = field(default_factory=dict)

    def location(self, name: str | None) -> str | None:
        cleaned = clean_name(name)
        return self.location_aliases.get(cleaned, cleaned) or None

    def course(self, name: str | None) -> str | None:
        cleaned = clean_name(name)
        return self.course_aliases.get(cleaned, cleaned) or None

    def context(self, name: str | None, course: str | None) -> str | None:
        cleaned = clean_name(name)
        cleaned = self.context_aliases.get(cleaned, cleaned)
        if not cleaned and course:
            cleaned = self.context_from_course.get(course, "")
        return cleaned or None


@dataclass(frozen=True)
class MapConfig:
    """The municipality map on the analysis page ([map] in config.toml)."""

    title: str
    attribution: str
    municipalities: list[Municipality]
    outside_label: str = "Außerhalb des Kartengebiets"
    labels: dict[str, str] = field(default_factory=dict)  # shorter names written on the map

    def label(self, name: str) -> str:
        return self.labels.get(name, name)


# Order and labels of school types when an instance does not configure them.
DEFAULT_SCHOOL_TYPES = {
    "GS": "Grundschule",
    "MS": "Mittelschule",
    "RS": "Realschule",
    "GYM": "Gymnasium",
    "MOS": "MOS",
    "FS": "Förderschule",
}


@dataclass(frozen=True)
class Project:
    """A selected project with its own page: the analysis, limited to its contexts."""

    slug: str
    title: str
    contexts: tuple[str, ...]
    # Planning: courses of this project record their participants per home municipality.
    participant_municipalities: bool = False


def load_projects(config: dict) -> list[Project]:
    projects = []
    for entry in config.get("projects", []):
        title = str(entry["title"]).strip()
        slug = str(entry.get("slug") or title).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            raise ValueError(f"[[projects]] {title!r}: slug must be lowercase letters, digits and dashes")
        contexts = tuple(entry.get("contexts") or [title])
        projects.append(Project(slug, title, contexts, bool(entry.get("participant_municipalities", False))))
    if len({p.slug for p in projects}) != len(projects):
        raise ValueError("[[projects]]: every project needs its own slug")
    return projects


@dataclass(frozen=True)
class Instance:
    path: Path
    school_year_start_month: int
    teacher_contexts: frozenset[str]
    slack_columns: dict[str, str]
    slack_ignored_columns: frozenset[str]
    locations: dict[str, bool]
    names: NameMappings
    theme: Theme
    instructor_aliases: dict[str, str] = field(default_factory=dict)
    instructor_ranking: bool = False
    location_school_types: dict[str, str] = field(default_factory=dict)
    location_municipalities: dict[str, str] = field(default_factory=dict)
    location_coordinates: dict[str, tuple[float, float]] = field(default_factory=dict)  # (lat, lon)
    school_types: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SCHOOL_TYPES))
    map: MapConfig | None = None
    projects: list[Project] = field(default_factory=list)

    def project(self, slug: str) -> Project | None:
        return next((p for p in self.projects if p.slug == slug), None)

    def instructor_key(self, recorded: str | None) -> str | None:
        """The person behind a recorded instructor value (first name or Slack user ID)."""
        cleaned = clean_name(recorded)
        return self.instructor_aliases.get(cleaned, cleaned) or None

    def location_is_school(self, location: str | None) -> bool | None:
        """True/False if the location is listed, None if unknown or empty."""
        return self.locations.get(clean_name(location))

    def school_type(self, location: str | None) -> str | None:
        """School type code of a listed school (e.g. "GS"), None if not set."""
        return self.location_school_types.get(clean_name(location))

    def municipality(self, location: str | None) -> str | None:
        """Municipality of a listed location, None if not set (e.g. "Online")."""
        return self.location_municipalities.get(clean_name(location))


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_theme(instance_dir: Path) -> Theme:
    path = instance_dir / "theme" / "tokens.toml"
    if not path.exists():
        path = DEFAULT_THEME
    tokens = _read_toml(path)
    fonts = tokens.get("font", {})
    categories = tokens.get("categories", {})
    return Theme(
        heading_font=fonts.get("heading", "monospace"),
        body_font=fonts.get("body", "sans-serif"),
        font_files=fonts.get("files", []),
        colors=tokens.get("color", {}),
        bar_opacity=float(tokens.get("chart", {}).get("bar_opacity", 0.9)),
        category_order=categories.get("order", []),
        category_colors=categories.get("colors", {}),
        palette=tokens.get("palette", {}).get("categorical", ["#4477aa", "#ee6677", "#ccbb44", "#228833"]),
        base_dir=path.parent,
        **({"map_gradient": tokens["map"]["gradient"]} if tokens.get("map", {}).get("gradient") else {}),
    )


def load_map(instance_dir: Path, config: dict) -> MapConfig | None:
    settings = config.get("map", {})
    if not settings.get("geojson"):
        return None
    return MapConfig(
        title=settings.get("title", "Gemeinden"),
        attribution=settings.get("attribution", ""),
        municipalities=load_municipalities(instance_dir / settings["geojson"]),
        outside_label=settings.get("outside_label", "Außerhalb des Kartengebiets"),
        labels=dict(settings.get("labels") or {}),
    )


def load_instance(instance_dir: Path) -> Instance:
    config = _read_toml(instance_dir / "config.toml")
    slack = config.get("slack", {})
    location_rows = _read_csv(instance_dir / "seeds" / "locations.csv")
    locations = {
        clean_name(row["name"]): row["school"].strip().lower() in ("ja", "yes", "true", "1")
        for row in location_rows
    }
    # Optional columns: older seed files have only name and school.
    school_types = {
        clean_name(row["name"]): row["school_type"].strip()
        for row in location_rows
        if (row.get("school_type") or "").strip()
    }
    municipalities = {
        clean_name(row["name"]): clean_name(row["municipality"])
        for row in location_rows
        if clean_name(row.get("municipality"))
    }
    coordinates = {
        clean_name(row["name"]): (float(row["lat"]), float(row["lon"]))
        for row in location_rows
        if (row.get("lat") or "").strip() and (row.get("lon") or "").strip()
    }
    # School types: seeds/school_types.csv (row order = chart order), else the defaults.
    school_type_rows = _read_csv(instance_dir / "seeds" / "school_types.csv")
    school_type_labels = (
        {row["code"].strip(): row["label"].strip() for row in school_type_rows}
        if school_type_rows
        else dict(DEFAULT_SCHOOL_TYPES)
    )
    mappings = instance_dir / "mappings"
    names = NameMappings(
        context_aliases={clean_name(r["alias"]): r["canonical"] for r in _read_csv(mappings / "kontext.csv")},
        context_from_course={
            clean_name(r["kursname"]): r["canonical"] for r in _read_csv(mappings / "kontext_inferred.csv")
        },
        course_aliases={clean_name(r["alias"]): r["canonical"] for r in _read_csv(mappings / "kursname.csv")},
        location_aliases={clean_name(r["alias"]): r["canonical"] for r in _read_csv(mappings / "ort.csv")},
    )
    return Instance(
        path=instance_dir,
        school_year_start_month=int(config.get("calendar", {}).get("school_year_start_month", 8)),
        teacher_contexts=frozenset(config.get("target_groups", {}).get("teacher_contexts", [])),
        slack_columns={**DEFAULT_SLACK_COLUMNS, **slack.get("columns", {})},
        slack_ignored_columns=frozenset(slack.get("ignored_columns", [])),
        locations=locations,
        names=names,
        theme=load_theme(instance_dir),
        instructor_aliases={
            clean_name(r["alias"]): r["slack_id"].strip()
            for r in _read_csv(instance_dir / "seeds" / "instructors.csv")
        },
        # Internal view; keep it off for anything public.
        instructor_ranking=bool(config.get("features", {}).get("instructor_ranking", False)),
        location_school_types=school_types,
        location_municipalities=municipalities,
        location_coordinates=coordinates,
        school_types=school_type_labels,
        map=load_map(instance_dir, config),
        projects=load_projects(config),
    )


@lru_cache
def get_instance() -> Instance:
    return load_instance(get_settings().instance_dir)
