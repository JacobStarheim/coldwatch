import pytest
from helpers import T0, Clock

from coldwatch.config import Settings
from coldwatch.db import Database
from coldwatch.engine import Engine


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def settings():
    return Settings(db_path=":memory:")


@pytest.fixture
def engine(settings, clock):
    return Engine(Database(":memory:"), settings, clock)
