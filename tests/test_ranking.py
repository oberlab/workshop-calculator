from metrics.ranking import NO_INSTRUCTOR, initials, instructor_ranking


def test_initials():
    assert initials("Erika Mustermann") == "EM"
    assert initials("Kim") == "K"
    assert initials("Anna-Lena Berg") == "AL"


def test_ranking_merges_aliases_and_sorts(instance):
    # instance.example maps the legacy first name "Kim" to the Slack user U0000000001.
    counts = [("Kim", 5), ("U0000000001", 7), ("Jo", 3), (None, 2), ("Ben", 3)]
    people = {"U0000000001": ("Kim Beispiel", True), "Jo": ("Jo", False)}
    rows = instructor_ranking(counts, instance, people)
    assert [(r.name, r.count) for r in rows] == [
        ("Kim Beispiel", 12),
        ("Ben", 3),
        ("Jo", 3),
        (NO_INSTRUCTOR, 2),
    ]
    assert rows[0].image_url == "/instructors/U0000000001/image"
    assert rows[1].image_url is None and rows[1].initials == "B"
    assert rows[-1].key is None


def test_image_url_is_quoted(instance):
    rows = instructor_ranking([("Thorsten S", 1)], instance, {"Thorsten S": ("Thorsten S", True)})
    assert rows[0].image_url == "/instructors/Thorsten%20S/image"


def test_feature_is_off_in_the_public_example(instance):
    assert instance.instructor_ranking is False
