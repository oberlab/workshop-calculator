from metrics.analytics import PersonCount
from metrics.ranking import NO_INSTRUCTOR, initials, instructor_ranking


def test_initials():
    assert initials("Erika Mustermann") == "EM"
    assert initials("Kim") == "K"
    assert initials("Anna-Lena Berg") == "AL"


def test_ranking_sorts_and_puts_missing_lead_last():
    counts = [
        PersonCount("U0000000001", "Kim Beispiel", True, 12),
        PersonCount("Jo", "Jo", False, 3),
        PersonCount(None, None, False, 2),
        PersonCount("Ben", "Ben", False, 3),
    ]
    rows = instructor_ranking(counts)
    assert [(r.name, r.count) for r in rows] == [
        ("Kim Beispiel", 12),
        ("Ben", 3),
        ("Jo", 3),
        (NO_INSTRUCTOR, 2),
    ]
    assert rows[0].image_url == "/instructors/U0000000001/image"
    assert rows[1].image_url is None and rows[1].initials == "B"
    assert rows[-1].key is None


def test_image_url_is_quoted():
    rows = instructor_ranking([PersonCount("Thorsten S", "Thorsten S", True, 1)])
    assert rows[0].image_url == "/instructors/Thorsten%20S/image"


def test_feature_is_off_in_the_public_example(instance):
    assert instance.instructor_ranking is False
