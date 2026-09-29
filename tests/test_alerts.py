"""Cichy kanał admina: błędy z logów → osobny czat Telegram, bez zalewu powtórzeniami."""

import logging
import sqlite3

import pytest

from gunb_tool.alerts import TelegramLogHandler, alert_headline, install_admin_alerts, telegram_sender
from gunb_tool.gunb_scraper import GunbFormatError
from gunb_tool.http_client import HttpError
from tests.fakes import FakeResponse, FakeSession

ULDK_DOWN = ("ULDK: 3 kolejne błędy (ostatni: GET https://uldk.gugik.gov.pl/?request=GetParcelByIdOrNr: "
             "wyczerpano limit ponowień (3); ostatni błąd: HTTP 500) – geokodowanie wstrzymane na 10 min")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def sent():
    return []


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def logger(sent, clock):
    handler = TelegramLogHandler(sent.append, clock=clock, hostname="vps")
    log = logging.getLogger("test.alerts")
    log.propagate = False
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    yield log
    log.removeHandler(handler)


def record_with(exc: BaseException, message: str = "Błąd: %s") -> logging.LogRecord:
    return logging.LogRecord("gunb_tool.test", logging.ERROR, __file__, 1, message, (exc,), (type(exc), exc, None))


# --- Co trafia do admina ------------------------------------------------------------------------

def test_errors_reach_admin_and_routine_logs_do_not(logger, sent):
    logger.info("Pobrano 166 leadów")
    logger.warning("GET https://uldk.gugik.gov.pl/: HTTP 503 – ponowienie 1/3 za 1.0 s")
    logger.error("Pobieranie danych GUNB nie powiodło się: brak połączenia")

    assert len(sent) == 1
    assert sent[0].startswith("🚨 BŁĄD:")
    assert "Pobieranie danych GUNB nie powiodło się" in sent[0]
    assert "vps" in sent[0]  # wiadomo, która maszyna (serwer czy komputer)


def test_alert_flag_forwards_recovery_notices(logger, sent):
    logger.warning("uldk.gugik.gov.pl znów odpowiada – wznawiam zapytania", extra={"alert": True})
    assert len(sent) == 1
    assert sent[0].startswith("✅") and "znów odpowiada" in sent[0]


# --- Czytelne nagłówki znanych awarii -----------------------------------------------------------

@pytest.mark.parametrize(
    "exc, expected",
    [
        (sqlite3.OperationalError("database is locked"), "Baza SQLite zablokowana"),
        (HttpError("GET https://wyszukiwarka.gunb.gov.pl/pliki_pobranie/wynik_x.zip: nieoczekiwany status HTTP 403",
                   status_code=403, url="https://wyszukiwarka.gunb.gov.pl/pliki_pobranie/wynik_x.zip"),
         "GUNB zablokował dostęp (HTTP 403) – możliwa blokada IP"),
        (HttpError("GET https://uldk.gugik.gov.pl/?request=x: wyczerpano limit ponowień (3); ostatni błąd: HTTP 500",
                   status_code=500, url="https://uldk.gugik.gov.pl/?request=x"), "ULDK zwraca HTTP 500"),
        (HttpError("GET https://uldk.gugik.gov.pl/: wyczerpano limit ponowień (3); ostatni błąd: Read timed out",
                   url="https://uldk.gugik.gov.pl/"), "ULDK nie odpowiada"),
        (GunbFormatError("wynik_x.zip: brak wymaganej kolumny"), "GUNB zmienił format danych"),
    ],
)
def test_known_failures_get_plain_headlines(exc, expected):
    assert alert_headline(record_with(exc)) == expected


def test_headline_is_recognised_from_message_text_alone():
    record = logging.LogRecord("gunb_tool.geocoding_uldk", logging.ERROR, __file__, 1, ULDK_DOWN, (), None)
    assert alert_headline(record) == "ULDK zwraca HTTP 500"


def test_unknown_error_uses_first_line_of_message():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "Coś nowego\nszczegóły", (), None)
    assert alert_headline(record) == "Coś nowego"


def test_exception_summary_is_included(logger, sent):
    try:
        raise sqlite3.OperationalError("database is locked")
    except sqlite3.OperationalError:
        logger.exception("Błąd zadania harmonogramu bota")
    assert sent[0].startswith("🚨 BŁĄD: Baza SQLite zablokowana")
    assert "OperationalError: database is locked" in sent[0]


# --- Bez zalewu powtórzeniami --------------------------------------------------------------------

def test_repeated_failure_is_grouped_within_window(logger, sent, clock):
    for _ in range(3):
        logger.error(ULDK_DOWN)
    assert len(sent) == 1

    clock.now += 3 * 3600 + 1
    logger.error(ULDK_DOWN)

    assert len(sent) == 2
    assert "powtórzyło się 2×" in sent[1]


def test_different_failures_are_not_grouped(logger, sent):
    logger.error(ULDK_DOWN)
    logger.error("Baza: database is locked")
    assert len(sent) == 2


# --- Bezpieczeństwo i odporność ------------------------------------------------------------------

def test_alert_is_short_and_contains_no_secrets(logger, sent):
    logger.error("Telegram: POST https://api.telegram.org/bot123456:SECRET-token/sendMessage – " + "x" * 6000)
    assert len(sent[0]) <= 4096
    assert "SECRET" not in sent[0]


def test_failed_delivery_never_breaks_the_program(capsys):
    def offline(text):
        raise OSError("brak sieci")

    handler = TelegramLogHandler(offline, hostname="vps")
    handler.handle(record_with(RuntimeError("awaria")))  # nie rzuca wyjątku
    assert "alert" in capsys.readouterr().err.lower()


def test_telegram_sender_posts_plain_text_to_admin_chat():
    session = FakeSession([FakeResponse(200, json_data={"ok": True})])
    telegram_sender("123:ABC", "-100777", session=session)("🚨 BŁĄD: test")
    call = session.calls[0]
    assert call.url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert call.json == {"chat_id": "-100777", "text": "🚨 BŁĄD: test", "disable_web_page_preview": True}


def test_telegram_sender_raises_when_telegram_rejects():
    session = FakeSession([FakeResponse(400, json_data={"ok": False, "description": "chat not found"})])
    with pytest.raises(RuntimeError, match="chat not found"):
        telegram_sender("123:ABC", "1", session=session)("x")


# --- Instalacja w logowaniu programu -------------------------------------------------------------

def test_install_without_admin_chat_is_a_no_op():
    assert install_admin_alerts("123:ABC", "") is None
    assert install_admin_alerts("", "42") is None


def test_install_forwards_errors_from_any_module_in_background():
    sent = []
    alerts = install_admin_alerts("123:ABC", "42", send=sent.append, hostname="vps")
    try:
        logging.getLogger("gunb_tool.pipeline").error("Awaria testowa")
    finally:
        alerts.stop()  # opróżnia kolejkę – alert o awarii tuż przed końcem programu nie ginie
    assert len(sent) == 1 and "Awaria testowa" in sent[0]
    assert not [h for h in logging.getLogger().handlers if getattr(h, "_gunb_tool_alerts", False)]
