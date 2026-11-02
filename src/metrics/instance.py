"""Organisation-specific configuration from the instance directory.

The instance directory is private (never in the public repository). Everything in
it is optional: without it the app runs with the neutral defaults in this package.

    instance/
    ├── config.toml          calendar, target groups, Slack column mapping
    ├── theme/tokens.toml    colours, fonts, category colours
    ├── theme/fonts/         font files referenced by tokens.toml
    ├── seeds/locations.csv  which locations are schools
    ├── seeds/instructors.csv  legacy first names -> Slack user IDs (same person)
    └── mappings/            name aliases (kontext.csv, kontext_inferred.csv, kursname.csv, ort.csv)
"""

import csv
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from metrics.config import get_settings

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

    def instructor_key(self, recorded: str | None) -> str | None:
        """The person behind a recorded instructor value (first name or Slack user ID)."""
        cleaned = clean_name(recorded)
        return self.instructor_aliases.get(cleaned, cleaned) or None

    def location_is_school(self, location: str | None) -> bool | None:
        """True/False if the location is listed, None if unknown or empty."""
        return self.locations.get(clean_name(location))


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
    )


def load_instance(instance_dir: Path) -> Instance:
    config = _read_toml(instance_dir / "config.toml")
    slack = config.get("slack", {})
    locations = {
        clean_name(row["name"]): row["school"].strip().lower() in ("ja", "yes", "true", "1")
        for row in _read_csv(instance_dir / "seeds" / "locations.csv")
    }
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
    )


@lru_cache
def get_instance() -> Instance:
    return load_instance(get_settings().instance_dir)
