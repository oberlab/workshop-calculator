"""Equipment and equipment conflicts (WM-140), on PostgreSQL with demo data only.

The Done-when check of the ticket: two Calliope courses on the same day with 20 + 15 of 30 sets
both show a critical hint, which goes away when one moves to another day.
"""

import datetime as dt
import html
import json
import re

import pytest
import sqlalchemy as sa

from metrics import equipment, hints, lists, messages
from metrics.db import (
    CONFIRMED,
    MERGED,
    PROPOSED,
    Appointment,
    AppointmentEquipment,
    Equipment,
    Format,
    FormatEquipment,
    HistoryEntry,
    StoredHint,
)
from metrics.planning import lanes, series, workflow
from tests.test_lists import _admin, _team
from tests.test_mine import NEXT_MONDAY, TODAY
from tests.test_workflow import World, form

TUESDAY, WEDNESDAY = NEXT_MONDAY + dt.timedelta(days=1), NEXT_MONDAY + dt.timedelta(days=2)


@pytest.fixture
def kit(db, instance):
    """The demo world plus 30 Calliope mini and a Calliope format that needs 20 of them."""
    from metrics.ingest import store

    store.seed_reference(db, instance)
    db.flush()
    w = World(db)
    w.calliope = Equipment(name="Calliope mini", quantity=30, status=CONFIRMED)
    w.laptops = Equipment(name="Laptopkoffer", quantity=2, status=CONFIRMED)
    w.calliope_format = Format(
        name="Calliope-Einsteiger", category="Code", duration_hours=3, status=CONFIRMED
    )
    db.add_all([w.calliope, w.laptops, w.calliope_format])
    db.flush()
    w.calliope_format.equipment = [FormatEquipment(equipment_id=w.calliope.id, quantity=20)]
    db.commit()
    return w


def _course(w, day: dt.date, **extra) -> Appointment:
    values = {"format_id": w.calliope_format.id, "date": day.isoformat()} | extra
    t = workflow.create(w.db, w.basics(**values), "Test")
    w.db.commit()
    return t


def _material(w, t: Appointment, **needs: int) -> dict:
    """The course form of `t` as the page sends it, with these needs (by attribute name of `w`)."""
    values = dict(
        format_id=t.course.format_id,
        context_id=t.course.context_id,
        place_id=t.course.place_id,
        class_kind="klasse",
        class_level=3,
        date=t.date.isoformat(),
        start_time=t.start_time.strftime("%H:%M"),
        end_time=t.end_time.strftime("%H:%M"),
        lead=t.lead_id or "",
        material="1",
    )
    values |= {f"material_{getattr(w, name).id}": n for name, n in needs.items()}
    return form(**values)


def _short(db, t: Appointment) -> list[StoredHint]:
    return list(
        db.scalars(
            sa.select(StoredHint).where(
                StoredHint.appointment_id == t.id, StoredHint.rule == "material-knapp"
            )
        )
    )


# --- Done when ----------------------------------------------------------------------------------


def test_done_when_two_calliope_courses_on_one_day(kit):
    w = kit
    a = _course(w, TUESDAY)  # 20 from the format
    b = _course(w, TUESDAY, start_time="13:00", end_time="15:00", lead=w.lea.id)
    assert equipment.needs_of(a.equipment) == equipment.needs_of(b.equipment) == {w.calliope.id: 20}
    assert _short(w.db, a) and _short(w.db, b)  # 40 of 30 already

    # The second course needs only 15: 20 + 15 of 30 is still short, on both, as Kritisch.
    workflow.save(w.db, b, _material(w, b, calliope=15), "Test")
    w.db.commit()
    (hint_a,), (hint_b,) = _short(w.db, a), _short(w.db, b)
    assert (hint_a.level, hint_b.level) == ("Kritisch", "Kritisch")
    assert hint_a.text.startswith("Calliope mini: 35 benötigt, 30 vorhanden")
    assert hint_b.text.startswith("Calliope mini: 35 benötigt, 30 vorhanden")

    # Moving one to another day clears the hint on both, also on the Termin left behind.
    moved = _material(w, b, calliope=15)
    moved["date"] = [WEDNESDAY.isoformat()]
    workflow.save(w.db, b, moved, "Test")
    w.db.commit()
    assert _short(w.db, a) == [] and _short(w.db, b) == []

    # The nightly run agrees.
    found = hints.refresh_all(w.db, TODAY)
    assert not [f for f in found if f.rule == "material-knapp"]


# --- The Material list ---------------------------------------------------------------------------


def test_material_list_with_proposals_and_admin_confirmation(kit, monkeypatch):
    w = kit
    team = _team(monkeypatch)
    assert team.get("/stammdaten/material").status_code == 200
    proposed = team.post(
        "/stammdaten/material", data={"name": "Lötstation", "quantity": "8"}, follow_redirects=False
    )
    assert proposed.status_code == 303
    item = w.db.scalars(sa.select(Equipment).where(Equipment.name == "Lötstation")).one()
    assert (item.status, item.quantity) == (PROPOSED, 8)
    page = team.get(f"/stammdaten/material?id={item.id}").text
    assert "Lötstation" in page and "/bestaetigen" not in page  # the decision is for admins
    assert team.post(f"/stammdaten/material/{item.id}/bestaetigen").status_code == 403
    assert (
        team.post("/stammdaten/material", data={"name": "Ohne Anzahl", "quantity": "viele"}).status_code
        == 422
    )

    admin = _admin(monkeypatch)
    assert (
        admin.post(f"/stammdaten/material/{item.id}/bestaetigen", follow_redirects=False).status_code == 303
    )
    w.db.refresh(item)
    assert item.status == CONFIRMED
    assert "Material" in team.get("/stammdaten/orte").text  # the list's tab and sidebar entry


def test_merging_equipment_adds_up_the_needs_and_keeps_the_entry(kit):
    w = kit
    t = _course(w, TUESDAY)
    duplicate = lists.propose(w.db, lists.EQUIPMENT, {"name": "Calliope", "quantity": 5}, "Kim").entry
    equipment.set_needs(w.db, t, {w.calliope.id: 20, duplicate.id: 4})
    w.db.flush()
    moved = lists.merge(w.db, lists.EQUIPMENT, duplicate, w.calliope, "Alex", "derselbe Satz")
    w.db.commit()
    assert moved == 1 and duplicate.status == MERGED and w.db.get(Equipment, duplicate.id) is not None
    rows = w.db.scalars(
        sa.select(AppointmentEquipment).where(AppointmentEquipment.appointment_id == t.id)
    ).all()
    assert [(r.equipment_id, r.quantity) for r in rows] == [(w.calliope.id, 24)]
    entry = w.db.scalars(
        sa.select(HistoryEntry).where(
            HistoryEntry.table_name == "appointments",
            HistoryEntry.field == "equipment",
            HistoryEntry.changed_by == "Alex",
        )
    ).all()
    assert [(e.old_value, e.new_value) for e in entry] == [("4 Calliope", "24 Calliope mini")]


# --- Needs per Format, copied to new Termine, changeable per Termin ------------------------------


def test_format_needs_through_the_stammdaten_page(kit, monkeypatch):
    w = kit
    url = f"/stammdaten/formate/{w.calliope_format.id}/material"
    # A confirmed format is changed by an admin, like its other details; a proposed one by the team.
    team = _team(monkeypatch)
    assert team.post(url, data={f"material_{w.laptops.id}": "1"}).status_code == 422
    fmt = lists.propose(w.db, lists.FORMATS, {"name": "Robotik", "category": "Code"}, "Kim").entry
    w.db.commit()
    saved = team.post(f"/stammdaten/formate/{fmt.id}/material", data={f"material_{w.laptops.id}": "2"})
    assert "Material gespeichert" in saved.text
    w.db.expire_all()
    assert equipment.needs_of(fmt.equipment) == {w.laptops.id: 2}

    admin = _admin(monkeypatch)  # from here on every client is an admin (the settings are shared)
    page = admin.get(f"/stammdaten/formate?id={w.calliope_format.id}").text
    assert f'action="{url}"' in page and f'name="material_{w.calliope.id}" value="20"' in page
    done = admin.post(url, data={f"material_{w.calliope.id}": "25", f"material_{w.laptops.id}": "1"})
    assert done.status_code == 200 and "Material gespeichert" in done.text
    w.db.expire_all()
    assert equipment.needs_of(w.calliope_format.equipment) == {w.calliope.id: 25, w.laptops.id: 1}
    entry = w.db.scalars(
        sa.select(HistoryEntry).where(
            HistoryEntry.field == "equipment", HistoryEntry.row_id == w.calliope_format.id
        )
    ).one()
    assert (entry.old_value, entry.new_value) == ("20 Calliope mini", "25 Calliope mini, 1 Laptopkoffer")


def test_new_termine_copy_the_needs_and_each_termin_can_change_them(kit):
    w = kit
    t = _course(w, TUESDAY)
    assert equipment.text(t.equipment) == "20 Calliope mini"
    entry = w.db.scalars(
        sa.select(HistoryEntry).where(
            HistoryEntry.table_name == "appointments",
            HistoryEntry.row_id == t.id,
            HistoryEntry.field == "equipment",
        )
    ).one()
    assert entry.new_value == "20 Calliope mini"

    # A series copies the Termin it is made from; changing one Termin leaves the others.
    later = series.add(w.db, t.course, [TUESDAY + dt.timedelta(weeks=1)], t, "Test")
    workflow.save(w.db, t, _material(w, t, calliope=12, laptops=1), "Test")
    w.db.commit()
    assert equipment.needs_of(t.equipment) == {w.calliope.id: 12, w.laptops.id: 1}
    assert equipment.needs_of(later[0].equipment) == {w.calliope.id: 20}
    # Forms without the Material fields (the phone form) leave it alone.
    workflow.save(w.db, t, w.basics(format_id=w.calliope_format.id, date=TUESDAY.isoformat()), "Test")
    assert equipment.needs_of(t.equipment) == {w.calliope.id: 12, w.laptops.id: 1}
    # "Dieser und alle folgenden" takes the needs to the following Termine.
    following = _material(w, t, calliope=10)
    following["umfang"] = ["folgende"]
    series.save(w.db, t, following, "Test", "folgende")
    assert equipment.needs_of(later[0].equipment) == {w.calliope.id: 10}
    # Nonsense is refused with a German message.
    with pytest.raises(workflow.FormError, match="ganze Zahl"):
        workflow.save(w.db, t, _material(w, t, calliope="viele"), "Test")
    # Digits Python's isdigit() takes but int() refuses are a form error too, not a crash (WM-140).
    with pytest.raises(workflow.FormError, match="ganze Zahl"):
        workflow.save(w.db, t, _material(w, t, calliope="²"), "Test")


def test_needs_follow_a_new_format_while_untouched(kit):
    w = kit
    plain = _course(w, TUESDAY, format_id=w.format.id)  # Löten needs nothing
    assert plain.equipment == []
    workflow.save(w.db, plain, w.basics(format_id=w.calliope_format.id, date=TUESDAY.isoformat()), "Test")
    assert equipment.needs_of(plain.equipment) == {w.calliope.id: 20}
    changed = _course(w, WEDNESDAY)
    workflow.save(w.db, changed, _material(w, changed, calliope=5), "Test")
    workflow.save(w.db, changed, w.basics(format_id=w.format.id, date=WEDNESDAY.isoformat()), "Test")
    assert equipment.needs_of(changed.equipment) == {w.calliope.id: 5}  # changed by hand: stays


def test_course_page_shows_the_needs_and_the_day(kit, monkeypatch):
    w = kit
    a = _course(w, TUESDAY)
    b = _course(w, TUESDAY, start_time="13:00", end_time="15:00", lead=w.lea.id)
    html = _team(monkeypatch).get(f"/planung/{b.course_id}?termin={b.id}").text
    material = html[html.index('id="material"') : html.index("</section>", html.index('id="material"'))]
    assert f'name="material_{w.calliope.id}" value="20"' in material
    assert "am Tag sonst 20 verplant" in material and "reicht am" in material
    assert "Calliope mini: 40 benötigt, 30 vorhanden" in html
    saved = _team(monkeypatch).post(
        f"/planung/{b.course_id}",
        data={**{k: v[0] for k, v in _material(w, b, calliope=10).items()}, "termin": b.id},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    w.db.expire_all()
    assert _short(w.db, a) == []


# --- Calendar lanes and the Slack reminder --------------------------------------------------------


def test_equipment_lanes_add_up_the_day(kit, monkeypatch):
    w = kit
    a = _course(w, TUESDAY)
    b = _course(w, TUESDAY, start_time="13:00", end_time="15:00", lead=w.lea.id)
    workflow.save(w.db, b, _material(w, b, calliope=15, laptops=1), "Test")
    c = _course(w, WEDNESDAY)
    w.db.commit()
    shown = equipment.booked_between(w.db, NEXT_MONDAY, NEXT_MONDAY + dt.timedelta(days=6))
    (week,) = lanes.equipment_weeks(
        shown, NEXT_MONDAY, NEXT_MONDAY + dt.timedelta(days=6), [w.calliope, w.laptops]
    )
    calliope, laptops = week.lanes
    tuesday, wednesday = calliope.days[1], calliope.days[2]
    assert (tuesday.needed, tuesday.over, tuesday.courses) == (35, True, 2)
    assert {t.id for t, _ in tuesday.termine} == {a.id, b.id}
    assert (wednesday.needed, wednesday.over, [t.id for t, _ in wednesday.termine]) == (20, False, [c.id])
    assert (laptops.days[1].needed, laptops.days[1].over) == (1, False)

    client = _team(monkeypatch)
    html = client.get(f"/kalender?datum={TUESDAY.isoformat()}&spuren=material").text
    assert "Material in KW" in html and "35 / 30 · 2 Kurse" in html and "reicht nicht" in html
    assert "20 / 30 · 1 Kurs" in html and f"/kalender/termin/{c.id}" in html
    assert client.get("/kalender?spuren=etwas").status_code == 400
    team = client.get(f"/team?datum={TUESDAY.isoformat()}").text
    assert "35 / 30 · 2 Kurse" in team
    panel = client.get(f"/kalender/termin/{b.id}", headers={"HX-Request": "true"}).text
    assert "15 Calliope mini, 1 Laptopkoffer" in panel


def test_reminder_names_the_equipment(kit):
    w = kit
    t = _course(w, TUESDAY)
    assert "• Material: 20 Calliope mini" in messages.reminder_text(t)


def test_material_boxes_untick_what_a_course_does_not_need(kit, monkeypatch):
    """Alex, 2026-11-19: a box per item; unticked, the item is not needed whatever number stands
    next to it; ticked without a number is an error. The Verlauf records the change."""
    w = kit
    t = _course(w, TUESDAY)
    assert equipment.needs_of(t.equipment) == {w.calliope.id: 20}
    team = _team(monkeypatch)
    page = team.get(f"/planung/{t.course_id}?termin={t.id}").text
    box = f'name="material_on_{w.calliope.id}" value="1" data-material-item="{w.calliope.id}" checked'
    assert box in page
    assert f'name="material_on_{w.laptops.id}" value="1" data-material-item="{w.laptops.id}">' in page
    assert 'name="material_checks"' in page

    def send(**extra):
        data = {k: v[0] for k, v in _material(w, t, calliope=20).items()}
        data |= {"termin": t.id, "material_checks": "1", **extra}
        return team.post(f"/planung/{t.course_id}", data=data, follow_redirects=False)

    assert send().status_code == 303  # Calliope not ticked: not needed for this course
    w.db.expire_all()
    t = w.db.get(Appointment, t.id)
    assert equipment.needs_of(t.equipment) == {}
    history = w.db.scalars(
        sa.select(HistoryEntry).where(
            HistoryEntry.table_name == "appointments",
            HistoryEntry.row_id == t.id,
            HistoryEntry.field == "equipment",
        )
    ).all()
    assert [(h.old_value, h.new_value) for h in history][-1] == ("20 Calliope mini", None)

    ticked = {f"material_on_{w.calliope.id}": "1", f"material_on_{w.laptops.id}": "1"}
    assert send(**ticked, **{f"material_{w.laptops.id}": "1"}).status_code == 303
    w.db.expire_all()
    assert equipment.needs_of(w.db.get(Appointment, t.id).equipment) == {w.calliope.id: 20, w.laptops.id: 1}
    refused = send(**{f"material_on_{w.laptops.id}": "1", f"material_{w.laptops.id}": ""})
    assert refused.status_code == 400 and "Laptopkoffer: angehakt, aber ohne Anzahl" in refused.text


def test_new_course_ticks_the_formats_material(kit, monkeypatch):
    """The new-course page knows what each format needs (planning.js ticks it); without the boxes
    being sent (`material` missing, as without JavaScript) the format's material is taken."""
    w = kit
    page = _team(monkeypatch).get("/planung/neu").text
    needs = re.search(r"data-format-needs='([^']*)'", page).group(1)
    assert json.loads(html.unescape(needs)) == {str(w.calliope_format.id): {str(w.calliope.id): 20}}
    t = _course(w, TUESDAY)  # the form without `material`: the format's material
    assert equipment.needs_of(t.equipment) == {w.calliope.id: 20}
    values = w.basics(format_id=w.calliope_format.id, date=TUESDAY.isoformat())
    unticked = workflow.create(w.db, values | {"material": ["1"], "material_checks": ["1"]}, "Test")
    assert equipment.needs_of(unticked.equipment) == {}  # unticked while creating
