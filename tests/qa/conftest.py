"""Wspólne fixtures testów QA – cały potok: GUNB (ZIP/CSV) → ULDK → SQLite (WAL) → Telegram.

Zasady:
* HTTP jest mockowane na poziomie transportu biblioteką ``responses`` – kod produkcyjny używa prawdziwej
  sesji ``requests``, prawdziwej polityki ponowień, bezpiecznika i obsługi ``Retry-After``;
* czas nie płynie naprawdę: ``sleep`` to ``Mock`` z ``pytest-mock`` (widać każdą przerwę i jej długość),
  a zegar monotoniczny jest sztuczny i przesuwa się o każdą „przespaną” chwilę;
* dane są syntetyczne (bez prawdziwych danych osobowych z rejestru).
"""

from __future__ import annotations

import io
import json
import random
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
import requests
import responses

from gunb_tool.config import HttpConfig
from gunb_tool.http_client import ResilientHttpClient
from gunb_tool.models import Investment
from gunb_tool.storage import LeadRepository
from gunb_tool.telegram_api import TelegramApi
from tests.gunb_fixtures import POZWOLENIA_HEADER, pozwolenie

GUNB_BASE = "https://gunb.test/pliki_pobranie/"
GUNB_OPOLSKIE_ZIP = GUNB_BASE + "wynik_opolskie.zip"
ULDK_URL = "https://uldk.test/"
BOT_TOKEN = "123456:QA-TOKEN"
TELEGRAM_SEND = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


class FakeClock:
    """Sztuczny zegar monotoniczny; ``advance`` przesuwa czas (np. o długość „przespanej” przerwy)."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleep(mocker, clock):
    """``time.sleep`` bez czekania: zapisuje wywołania i przesuwa sztuczny zegar."""
    return mocker.Mock(name="sleep", side_effect=clock.advance)


def slept(sleep_mock) -> list[float]:
    """Długości kolejnych przerw zarejestrowanych przez mock ``sleep``."""
    return [call.args[0] for call in sleep_mock.call_args_list]


@pytest.fixture
def http():
    """Aktywny mock transportu HTTP (``responses``); każdy test rejestruje własne odpowiedzi."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def make_http(sleep, clock) -> Callable[..., ResilientHttpClient]:
    """Klient HTTP jak w produkcji (3 ponowienia, backoff 2 → 4 → 8 s, bezpiecznik), bez realnego czekania."""
    def factory(**overrides: Any) -> ResilientHttpClient:
        params: dict[str, Any] = dict(timeout=5, max_retries=3, backoff_base=2, backoff_max=60,
                                      min_delay=0.0, max_delay=0.0)
        params.update(overrides)
        return ResilientHttpClient(HttpConfig(**params), session=requests.Session(), sleep=sleep, clock=clock,
                                   rng=random.Random(7))
    return factory


@pytest.fixture
def gunb_zip() -> Callable[..., bytes]:
    """Buduje paczkę ZIP w formacie GUNB (CSV rozdzielany średnikami)."""
    def build(rows: list[dict[str, str]] | None = None, *, header: list[str] | None = None,
              encoding: str = "utf-8-sig", compression: int = zipfile.ZIP_DEFLATED) -> bytes:
        header = header or POZWOLENIA_HEADER
        lines = [";".join(header)]
        for row in rows if rows is not None else [pozwolenie()]:
            lines.append(";".join(row.get(column, "") for column in header))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
            archive.writestr("wynik_opolskie.csv", ("\n".join(lines) + "\n").encode(encoding))
        return buffer.getvalue()
    return build


@pytest.fixture
def qa_workdir(tmp_path, monkeypatch) -> Path:
    """Katalog z konfiguracją wskazującą na zamockowane GUNB i ULDK (bez prawdziwych sekretów z otoczenia)."""
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(
        f"gunb:\n  base_url: {GUNB_BASE}\n  sources: [pozwolenia]\n  voivodeships: ['16']\n"
        "  powiats: ['1607']\n  cache_dir: data/cache\n"
        f"geocoding:\n  uldk_url: {ULDK_URL}\n  min_delay: 0\n  max_delay: 0\n"
        # szybkie ponowienia w testach całego programu (tu sleep jest prawdziwy)
        "http:\n  max_retries: 3\n  backoff_base: 0.001\n  backoff_max: 0.005\n  min_delay: 0\n  max_delay: 0\n"
        "storage:\n  db_path: data/qa.sqlite\n"
        "logging:\n  level: INFO\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def fixed_now() -> Callable[[], datetime]:
    return lambda: datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def memory_repo(fixed_now):
    """Baza w pamięci (``sqlite:///:memory:``) – każdy test dostaje czystą."""
    repository = LeadRepository(":memory:", now=fixed_now)
    yield repository
    repository.close()


@pytest.fixture
def db_path(tmp_path) -> Path:
    """Plik bazy na dysku – potrzebny do testów współbieżności (WAL działa tylko na plikach)."""
    return tmp_path / "leady.sqlite"


@pytest.fixture
def make_lead() -> Callable[..., Investment]:
    def build(id_sprawy: str = "ST-OP-NY/WNIOSEK/1/2026", **overrides: Any) -> Investment:
        base: dict[str, Any] = dict(
            id_sprawy=id_sprawy, zrodlo="pozwolenia", status="decyzja", data_aktualizacji="2026-09-25",
            kategoria="mieszkaniowa-jednorodzinna", nazwa_zamierzenia="Budowa budynku mieszkalnego jednorodzinnego",
            adres_opisowy="ul. Testowa 5, Nysa", miejscowosc="Nysa", powiat="powiat nyski", powiat_teryt="1607",
            gmina_teryt="1607054", kubatura=650.0, priorytet="normal", punkty=3,
            lat=50.47, lon=17.33, google_maps_url="https://www.google.com/maps?q=50.470000,17.330000",
        )
        base.update(overrides)
        return Investment(**base)
    return build


@pytest.fixture
def telegram_api(make_http, sleep, clock) -> TelegramApi:
    """Prawdziwy klient Bot API na zamockowanym transporcie (bez bezpiecznika – jak w pętli bota)."""
    return TelegramApi(make_http(circuit_breaker_failures=0), BOT_TOKEN, sleep=sleep, clock=clock)


def telegram_ok(message_id: int = 1) -> dict[str, Any]:
    return {"ok": True, "result": {"message_id": message_id, "chat": {"id": 0}, "date": 0}}


def telegram_error(code: int, description: str, **parameters: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return body


def sent_payloads(mock: responses.RequestsMock, url: str = TELEGRAM_SEND) -> list[dict[str, Any]]:
    """Treści (JSON) wszystkich zapytań wysłanych pod ``url`` – w kolejności wysyłki."""
    return [json.loads(call.request.body) for call in mock.calls if call.request.url == url]
