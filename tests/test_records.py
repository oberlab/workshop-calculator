import pytest

from metrics.ingest import records as r


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Kl. 3 / 8 Jahre", r.ClassAge(class_level=3, age=8)),
        ("Kl. 10 / 15 Jahre", r.ClassAge(class_level=10, age=15)),
        ("19 - 24 Jahre", r.ClassAge(age_band="19 - 24 Jahre")),
        ("über 65 Jahre", r.ClassAge(age_band="über 65 Jahre")),
        ("Lehrkräfte", r.ClassAge(teachers=True)),
        ("", r.ClassAge()),
        (None, r.ClassAge()),
        ("irgendwas", r.ClassAge(parsed=False)),
    ],
)
def test_parse_class_age(text, expected):
    assert r.parse_class_age(text) == expected


def group(class_age, location_is_school=True, context=None):
    return r.target_group(
        context=context,
        class_age=class_age,
        location_is_school=location_is_school,
        teacher_contexts=frozenset({"LK"}),
    )


def test_target_group_rules():
    assert group(r.ClassAge(class_level=3, age=8)) == r.SCHOOL_CLASS
    assert group(r.ClassAge(class_level=3, age=8), location_is_school=False) == r.CHILDREN_OUTSIDE_SCHOOL
    assert group(r.ClassAge(class_level=3, age=8), location_is_school=None) == r.UNKNOWN
    assert group(r.ClassAge(age=6), location_is_school=False) == r.CHILDREN_OUTSIDE_SCHOOL
    assert group(r.ClassAge(age=18), location_is_school=False) == r.CHILDREN_OUTSIDE_SCHOOL
    assert group(r.ClassAge(age=45)) == r.ADULTS
    assert group(r.ClassAge(age_band="19 - 24 Jahre")) == r.ADULTS
    assert group(r.ClassAge(age_band="über 65 Jahre")) == r.ADULTS
    assert group(r.ClassAge(teachers=True)) == r.TEACHERS
    assert group(r.ClassAge(class_level=9, age=14), context="LK") == r.TEACHERS
    assert group(r.ClassAge()) == r.UNKNOWN


def test_number_parsing():
    assert r.to_int("NA") is None
    assert r.to_int("3,0") == 3
    assert str(r.to_decimal("1,5")) == "1.5"
    assert r.to_text("  Repair  Café ") == "Repair Café"
    with pytest.raises(ValueError):
        r.to_decimal("abc")
