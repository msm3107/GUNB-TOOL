"""Atrapy wykorzystywane w testach (bez sieci)."""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any, Iterable

from requests.structures import CaseInsensitiveDict

from gunb_tool.config import HttpConfig
from gunb_tool.http_client import ResilientHttpClient


class FakeResponse:
    """Minimalny odpowiednik ``requests.Response`` używany przez klienta HTTP."""

    def __init__(
        self,
        status_code: int = 200,
        body: bytes | str = b"",
        headers: dict[str, str] | None = None,
        chunks: Iterable[bytes | Exception] | None = None,
        json_data: Any = None,
    ) -> None:
        self.status_code = status_code
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = CaseInsensitiveDict(headers or {})
        self._chunks = list(chunks) if chunks is not None else None
        self._json = json_data
        self.closed = False

    @property
    def content(self) -> bytes:
        return self._body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8")

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("brak JSON")
        return self._json

    def iter_content(self, chunk_size: int = 1):
        for chunk in self._chunks if self._chunks is not None else [self._body]:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """Sesja zwracająca kolejno zaplanowane odpowiedzi (lub rzucająca wyjątki)."""

    def __init__(self, script: Iterable[FakeResponse | Exception]) -> None:
        self.script = list(script)
        self.calls: list[SimpleNamespace] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(SimpleNamespace(method=method, url=url, **kwargs))
        if not self.script:
            raise AssertionError(f"Nieoczekiwane zapytanie {method} {url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeTime:
    """Sztuczny zegar: ``sleep`` przesuwa czas i zapamiętuje długość przerw."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(
    script: Iterable[FakeResponse | Exception],
    fake_time: FakeTime | None = None,
    **config: Any,
) -> tuple[ResilientHttpClient, FakeSession, FakeTime]:
    """Tworzy klienta HTTP z atrapą sesji i zegara (domyślnie bez opóźnień)."""
    fake_time = fake_time or FakeTime()
    params = dict(min_delay=0.0, max_delay=0.0, max_retries=3, backoff_base=1.0, backoff_max=8.0)
    params.update(config)
    session = FakeSession(script)
    client = ResilientHttpClient(
        HttpConfig(**params),
        session=session,
        sleep=fake_time.sleep,
        clock=fake_time.clock,
        rng=random.Random(42),
    )
    return client, session, fake_time
