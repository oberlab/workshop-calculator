from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from metrics import analytics
from metrics.db import Base, SessionRow


def row(n, when, location, number, total, status="Durchgeführt", category="Code"):
    return SessionRow(
        source="test",
        source_key=f"k{n}",
        date=when,
        location=location,
        session_number=number,
        participants_total=total,
        status=status,
        category=category,
        target_group="Schulklasse",
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                row(1, date(2025, 3, 1), "Schule A", 1, 20),
                row(2, date(2025, 3, 8), "Schule A", 2, 20),  # same course, second session: not counted again
                row(3, date(2025, 3, 9), "Schule B", 1, 12),
                row(4, date(2025, 4, 1), None, 1, 5),  # no location: not a place
                row(5, date(2025, 4, 2), "Schule B", None, 8),  # no session number: not counted
                row(6, date(2025, 5, 1), "Schule C", 1, 30, status="In Planung"),  # not held
                row(7, date(2024, 5, 1), "Schule D", 1, 9, category="Science"),
            ]
        )
        session.commit()
        yield session


def test_totals(db):
    result = analytics.totals(db, analytics.Filters(), date(2026, 1, 1), 8)
    assert result == analytics.Totals(sessions=6, participants=46, locations=3)


def test_totals_follow_filters(db):
    filters = analytics.Filters(
        preset="custom", date_from=date(2025, 1, 1), date_to=date(2025, 12, 31), categories=["Code"]
    )
    assert analytics.totals(db, filters, date(2026, 1, 1), 8) == analytics.Totals(5, 37, 2)
