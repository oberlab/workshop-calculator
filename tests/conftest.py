from pathlib import Path

import pytest

from metrics.instance import Instance, load_instance

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def instance() -> Instance:
    """The public example instance: synthetic locations, mappings and theme."""
    return load_instance(ROOT / "instance.example")
