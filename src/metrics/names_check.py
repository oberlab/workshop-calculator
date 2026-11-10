"""Flag names in new entries that differ from the clean data, never change them.

Imports store course names, locations and contexts exactly as recorded (only whitespace is
tidied). `metrics check-names` lists every value in the synced Slack entries that the clean data
does not use, with a suggestion: the alias recorded in the instance mappings, or else the most
similar known name. A person decides: fix the entry in Slack, or record a decision.
"""

import difflib
from collections import Counter
from dataclasses import dataclass, field

from metrics.instance import Instance

FIELDS = {"course_name": "Kursname", "location": "Ort", "context": "Kontext"}


@dataclass
class Divergent:
    value: str
    sessions: int
    suggestion: str | None


@dataclass
class NamesReport:
    divergent: dict[str, list[Divergent]] = field(default_factory=dict)  # field -> values

    @property
    def problems(self) -> int:
        return sum(len(values) for values in self.divergent.values())


def _aliases(instance: Instance, name: str) -> dict[str, str]:
    names = instance.names
    return {
        "course_name": names.course_aliases,
        "location": names.location_aliases,
        "context": names.context_aliases,
    }[name]


def suggestion(value: str, known: set[str], aliases: dict[str, str]) -> str | None:
    if value in aliases:
        return aliases[value]
    close = difflib.get_close_matches(value, sorted(known), n=1, cutoff=0.75)
    if close:
        return close[0]
    folded = {k.casefold(): k for k in known}
    return folded.get(value.casefold())


def check_names(instance: Instance, known: dict[str, set[str]], new: dict[str, Counter]) -> NamesReport:
    """known: field -> names of the clean data; new: field -> Counter of values in new entries."""
    report = NamesReport()
    for name in FIELDS:
        vocabulary = set(known.get(name, set()))
        if name == "location":
            vocabulary |= set(instance.locations)
        values = [
            Divergent(value, count, suggestion(value, vocabulary, _aliases(instance, name)))
            for value, count in sorted(new.get(name, Counter()).items(), key=lambda item: (-item[1], item[0]))
            if value and value not in vocabulary
        ]
        if values:
            report.divergent[name] = values
    return report


def format_report(report: NamesReport) -> str:
    lines = []
    for name, label in FIELDS.items():
        values = report.divergent.get(name, [])
        lines.append(f"{label}: {len(values)} not in the clean data")
        for d in values:
            hint = f" -> suggestion: {d.suggestion}" if d.suggestion else ""
            lines.append(f"  {d.value!r} ({d.sessions} entries){hint}")
    lines.append(f"Names to decide: {report.problems}")
    return "\n".join(lines)
