"""Wspólne fikstury testów silnika."""

from datetime import datetime, timedelta, timezone
import pytest

from core.storage import LeadRepository


class Clock:
    """Prosty zegar testowy z możliwością przesuwania czasu."""

    def __init__(self, current: datetime | None = None) -> None:
        self.current = current or datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, **delta) -> None:
        self.current += timedelta(**delta)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def repo(clock):
    repository = LeadRepository(":memory:", now=clock.now_utc)
    yield repository
    repository.close()
