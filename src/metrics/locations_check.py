"""Consistency checks for the location lookup tables (seeds/locations.csv, seeds/school_types.csv).

Run with `metrics check-locations`. Reports only; it never changes the tables.
"""

from dataclasses import dataclass, field

from metrics.geo import municipality_at
from metrics.instance import Instance


@dataclass
class LocationReport:
    not_in_lookup: dict[str, int] = field(default_factory=dict)  # location -> held sessions
    school_without_type: list[str] = field(default_factory=list)
    unknown_school_type: dict[str, str] = field(default_factory=dict)  # location -> code
    # location -> (municipality column, municipality found from the coordinates)
    municipality_mismatch: dict[str, tuple[str, str]] = field(default_factory=dict)
    without_municipality: dict[str, int] = field(default_factory=dict)  # location -> held sessions

    @property
    def problems(self) -> int:
        return (
            len(self.not_in_lookup)
            + len(self.school_without_type)
            + len(self.unknown_school_type)
            + len(self.municipality_mismatch)
        )


def check_locations(instance: Instance, sessions_per_location: dict[str | None, int]) -> LocationReport:
    report = LocationReport()
    map_names = {m.name for m in instance.map.municipalities} if instance.map else set()

    for location, sessions in sorted(sessions_per_location.items(), key=lambda item: -item[1]):
        if location and instance.location_is_school(location) is None:
            report.not_in_lookup[location] = sessions
        if not instance.municipality(location):
            report.without_municipality[location or "(ohne Ort)"] = sessions

    for name, is_school in sorted(instance.locations.items()):
        code = instance.school_type(name)
        if is_school and not code:
            report.school_without_type.append(name)
        elif code and code not in instance.school_types:
            report.unknown_school_type[name] = code

        if instance.map and name in instance.location_coordinates:
            lat, lon = instance.location_coordinates[name]
            from_coordinates = municipality_at(instance.map.municipalities, lon, lat)
            column = instance.municipality(name) or ""
            if from_coordinates and column != from_coordinates:
                report.municipality_mismatch[name] = (column, from_coordinates)
            elif not from_coordinates and column in map_names:
                report.municipality_mismatch[name] = (column, "außerhalb der Karte")
    return report


def format_report(report: LocationReport) -> str:
    lines = []

    def section(title: str, items: list[str]) -> None:
        lines.append(f"{title}: {len(items)}")
        lines.extend(f"  {item}" for item in items)

    section(
        "Locations with held sessions missing from seeds/locations.csv",
        [f"{name} ({n} sessions)" for name, n in report.not_in_lookup.items()],
    )
    section("Schools without school_type", report.school_without_type)
    section(
        "School types missing from seeds/school_types.csv",
        [f"{name}: {code}" for name, code in report.unknown_school_type.items()],
    )
    section(
        "Municipality column does not match the coordinates",
        [
            f"{name}: column '{column}', coordinates '{found}'"
            for name, (column, found) in report.municipality_mismatch.items()
        ],
    )
    section(
        "Held sessions without a municipality (shown beside the map)",
        [f"{name} ({n} sessions)" for name, n in report.without_municipality.items()],
    )
    lines.append(f"Problems: {report.problems}")
    return "\n".join(lines)
