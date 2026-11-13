"""Design system: the navigation entries per role and the component macros (WM-104)."""

import datetime as dt
import re

from metrics import auth
from metrics.instance import Project
from metrics.web import nav
from metrics.web.common import templates

TEAM = auth.Login("Kim", admin=False, subject="kim")
ADMIN = auth.Login("Alex", admin=True, subject="alex")
PROJECTS = [Project("projekt-a", "Projekt A", ("Projekt A",))]


def _links(groups):
    return {link.label: (group, link) for group, links in groups for link in links}


def test_public_sees_only_the_overview():
    groups = nav.sidebar(None, "/", {}, PROJECTS)
    assert [(g, [link.label for link in links]) for g, links in groups] == [(None, ["Übersicht"])]
    assert groups[0][1][0].note is None and groups[0][1][0].active


def test_team_and_admin_entries_in_their_groups():
    team = _links(nav.sidebar(TEAM, "/analyse", {}, PROJECTS))
    assert team["Kurse"][0] == nav.PLANUNG and team["Projekt A"][0] == nav.AUSWERTUNG
    assert team["Analyse"][1].active and "Review" not in team
    assert team["Übersicht"][1].note == "öffentlich"
    admin = _links(nav.sidebar(ADMIN, "/review", {}, PROJECTS))
    assert admin["Review"] == (nav.VERWALTUNG, admin["Review"][1]) and admin["Review"][1].active


def test_planned_pages_stay_hidden():
    labels = _links(nav.sidebar(ADMIN, "/", {}, PROJECTS))
    for entry in nav.ENTRIES:
        if isinstance(entry, nav.Entry) and entry.planned:
            assert entry.label not in labels


def test_longest_matching_address_is_active():
    def active(path):
        return [link.label for _, links in nav.sidebar(TEAM, path, {}, []) for link in links if link.active]

    assert active("/planung/12") == ["Kurse"]
    assert active("/planung/orte") == ["Orte"]
    assert active("/planung/neu") == ["+ Neuer Kurs"]
    assert active("/") == ["Übersicht"]
    assert active("/planungx") == []


def test_counts_only_above_zero():
    counts = {"courses_high": 2, "courses_medium": 0, "places": 1}
    links = _links(nav.sidebar(TEAM, "/", counts, []))
    assert links["Kurse"][1].counts == [(2, "critical", "Kurse mit kritischem Hinweis")]
    assert links["Orte"][1].counts == [(1, "warning", "Orte ohne Adresse")]


def _render(source: str, **context) -> str:
    return templates.env.from_string('{% import "_ui.html" as ui %}' + source).render(**context)


def test_component_macros_render():
    html = _render(
        """
        {{ ui.button("Speichern") }}{{ ui.button("Liste", href="/planung", kind="secondary", size="sm") }}
        {{ ui.count(3, "critical", "Kurse") }}{{ ui.stage_pill("bestaetigt", "Bestätigt") }}
        {{ ui.hint_badge("high") }}{{ ui.hint_badge("low", "2. Kurs am Tag") }}
        {{ ui.avatar("Mia Muster", "sm") }}
        {{ ui.field("Datum", "date", type="date", required=true, help="Pflicht ab Bestätigt") }}
        {% call ui.table(["Nr.", ""]) %}<tr><td>1</td><td></td></tr>{% endcall %}
        {% call ui.panel("Termine", n=4) %}<p>x</p>{% endcall %}
        {% call ui.card(day, "JugendLab", ["16:00–18:00", "Oberlab"], stage="geplant", href="/planung/1") %}
          {{ ui.stage_pill("geplant", "Geplant") }}{% endcall %}
        {{ ui.side_panel("kurs", "Kurs") }}
        {{ ui.tabs([("a", "Geplant", "?stage=a", 2), ("b", "Bestätigt", "?stage=b")], "a", "Phasen") }}
        {{ ui.toast("Gespeichert.") }}{{ ui.alert(["Fehlt: Ort"], "error") }}
        {% call ui.empty_state("Keine Kurse.") %}{% endcall %}
        {{ ui.workload_bar(4, 3) }}{{ ui.workload_bar(2, 5, support=1) }}{{ ui.workload_bar(0, 0) }}
        """,
        day=dt.date(2026, 11, 16),
    )
    assert '<button class="btn" type="button">Speichern</button>' in html
    assert '<a class="btn btn-secondary btn-sm" href="/planung">Liste</a>' in html
    assert 'class="count count-critical" title="3 Kurse"' in html
    assert '<span class="pill pill-bestaetigt">Bestätigt</span>' in html
    assert "hint-badge hint-high" in html and "Kritisch" in html and "Info: </span>2. Kurs am Tag" in html
    assert re.search(r'class="avatar tone-\d avatar-sm" aria-hidden="true">MM<', html)
    assert 'type="date" name="date" value="" aria-required="true"' in html
    assert '<th scope="col"><span class="visually-hidden">Aktionen</span></th>' in html
    assert '<a class="card stage-geplant" href="/planung/1">' in html and "<b>16</b><span>Nov</span>" in html
    assert '<dialog class="side-panel" id="kurs"' in html and 'id="kurs-body"' in html
    assert 'href="?stage=a" aria-current="page">Geplant' in html
    assert 'class="toast" role="status"' in html and 'role="alert"' in html
    assert 'class="workload is-over"' in html and "w-100" in html and "4 / 3" in html
    assert 'class="workload"' in html and "w-40" in html and "is-support w-20" in html
    assert "style=" not in html  # the CSP blocks inline styles


def test_no_inline_styles_in_templates():
    from pathlib import Path

    for template in (Path(nav.__file__).parent / "templates").glob("*.html"):
        html = template.read_text()
        assert not re.search(r"\sstyle\s*=", html), template.name
        assert "<style" not in html, template.name
