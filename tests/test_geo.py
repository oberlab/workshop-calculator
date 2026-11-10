from metrics.geo import _signed_area, label_point, rewind_for_drawing

# A square as GeoJSON writes it: outer ring counter-clockwise, with a clockwise hole.
SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
        [[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]],
    ],
}


def test_rewind_makes_outer_rings_clockwise_and_holes_counter_clockwise():
    rewound = rewind_for_drawing(SQUARE)
    assert rewound["type"] == "MultiPolygon"
    outer, hole = rewound["coordinates"][0]
    assert _signed_area(outer) < 0
    assert _signed_area(hole) > 0


def test_rewind_keeps_rings_that_are_already_right():
    once = rewind_for_drawing(SQUARE)
    assert rewind_for_drawing(once) == once


def test_label_point_is_the_centroid_of_the_largest_part():
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            [[[10, 10], [14, 10], [14, 12], [10, 12], [10, 10]]],
        ],
    }
    lon, lat = label_point(geometry)
    assert (round(lon, 6), round(lat, 6)) == (12, 11)


def test_contains_respects_holes():
    from metrics.geo import contains

    assert contains(SQUARE, 3, 3)
    assert not contains(SQUARE, 1.5, 1.5)  # in the hole
    assert not contains(SQUARE, 5, 5)


def test_municipality_at_example_map(instance):
    from metrics.geo import municipality_at

    municipalities = instance.map.municipalities
    assert municipality_at(municipalities, 11.1, 48.05) == "Musterstadt"
    assert municipality_at(municipalities, 11.3, 48.2) == "Neudorf"
    assert municipality_at(municipalities, 12.0, 48.0) is None
