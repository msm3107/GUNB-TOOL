"""Wspólne fikstury testów bota (moduły z własnymi fiksturami o tych nazwach je przesłaniają)."""

import pytest

from gunb_tool.storage import LeadRepository
from tests.bot_helpers import Clock, FakeApi


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def repo(clock):
    repository = LeadRepository(":memory:", now=clock.now_utc)
    yield repository
    repository.close()


@pytest.fixture
def api():
    return FakeApi()
