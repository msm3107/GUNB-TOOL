"""Cichy kanał administracyjny: awarie z logów trafiają na osobny czat Telegram admina.

Klient („pan Mietek”) dostaje wyłącznie leady. Awarie – „GUNB zablokował dostęp”, „ULDK zwraca HTTP 500”,
„Baza SQLite zablokowana” – idą na ``TELEGRAM_ADMIN_CHAT_ID``. Mechanizmem jest zwykły ``logging``:

* :class:`TelegramLogHandler` przekazuje rekordy od poziomu ERROR oraz rekordy oznaczone
  ``extra={"alert": True}`` (np. „ULDK znów odpowiada”), z czytelnym nagłówkiem rozpoznanej awarii,
* ta sama awaria powtarzająca się w ciągu 3 godzin daje jedną wiadomość (kolejna ma licznik powtórzeń);
  „ta sama” = ten sam nagłówek po zamianie liczb (ID czatu, numer aktualizacji), więc błąd u 200 osób
  to jeden alert,
* najwyżej ``MAX_PER_HOUR`` alertów na godzinę – przy lawinie różnych awarii reszta zostaje w logu,
  a następny alert mówi, ile pominięto,
* wysyłka idzie w osobnym wątku – awaria sieci nie spowalnia bota, a nieudany alert nigdy nie
  przerywa programu ani nie wywołuje kolejnego alertu.
"""

from __future__ import annotations

import logging
import queue
import re
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator

import requests

from .gunb_scraper import GunbFormatError
from .http_client import RETRYABLE_STATUS, HttpError, redact_url

TELEGRAM_LIMIT = 3900  # Telegram: 4096 znaków; zapas na emoji liczone podwójnie
DETAIL_LIMIT = 300
DEDUP_SECONDS = 3 * 3600
MAX_PER_HOUR = 20
_SERVICES = (
    ("gunb.gov.pl", "GUNB"),
    ("uldk.gugik.gov.pl", "ULDK"),
    ("api.telegram.org", "Telegram"),
    ("discord.com", "Discord"),
)
_HTTP_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")
_NUMBERS_RE = re.compile(r"\d+")

Sender = Callable[[str], None]


def alert_headline(record: logging.LogRecord) -> str:
    """Krótki opis awarii dla admina, np. „ULDK zwraca HTTP 500” (albo pierwsza linia komunikatu)."""
    for error in _exception_chain(record.exc_info[1] if record.exc_info else None):
        if isinstance(error, sqlite3.OperationalError) and "locked" in str(error):
            return "Baza SQLite zablokowana"
        if isinstance(error, GunbFormatError):
            return "GUNB zmienił format danych"
        if isinstance(error, HttpError):
            return _http_headline(f"{error.url or ''} {error}", error.status_code)
    text = record.getMessage()
    if "database is locked" in text:
        return "Baza SQLite zablokowana"
    if any(host in text for host, _ in _SERVICES):
        return _http_headline(text, None)
    return _first_line(text)


class TelegramLogHandler(logging.Handler):
    """Handler ``logging`` wysyłający awarie na czat admina (patrz opis modułu).

    Args:
        send: funkcja wysyłająca gotowy tekst (np. :func:`telegram_sender`).
        min_level: od tego poziomu rekord jest alertem.
        dedup_seconds: w tym oknie ta sama awaria nie jest wysyłana ponownie.
        max_per_hour: najwięcej alertów w godzinie (reszta tylko w logu).
        clock: zegar monotoniczny (wstrzykiwany w testach).
        hostname: nazwa maszyny w alercie (serwer czy komputer).
    """

    def __init__(
        self,
        send: Sender,
        *,
        min_level: int = logging.ERROR,
        dedup_seconds: float = DEDUP_SECONDS,
        max_per_hour: int = MAX_PER_HOUR,
        clock: Callable[[], float] = time.monotonic,
        hostname: str | None = None,
    ) -> None:
        super().__init__(logging.NOTSET)
        self._send = send
        self.min_level = min_level
        self.dedup_seconds = dedup_seconds
        self._clock = clock
        self.hostname = hostname or socket.gethostname()
        self.max_per_hour = max_per_hour
        self._last_sent: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._hour_start = float("-inf")
        self._sent_this_hour = 0
        self._dropped = 0
        self.addFilter(self.is_alert)

    def is_alert(self, record: logging.LogRecord) -> bool:
        """Czy rekord ma trafić do admina."""
        return record.levelno >= self.min_level or bool(getattr(record, "alert", False))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            is_error = record.levelno >= self.min_level
            headline = alert_headline(record) if is_error else _first_line(record.getMessage())
            key = _NUMBERS_RE.sub("#", headline)
            now = self._clock()
            last = self._last_sent.get(key)
            if last is not None and now - last < self.dedup_seconds:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return
            if now - self._hour_start >= 3600:
                self._hour_start, self._sent_this_hour = now, 0
            if self._sent_this_hour >= self.max_per_hour:
                self._dropped += 1
                return
            self._sent_this_hour += 1
            if len(self._last_sent) > 500:  # proces działa miesiącami – stare wpisy nie są już potrzebne
                self._last_sent = {k: t for k, t in self._last_sent.items() if now - t < self.dedup_seconds}
            self._last_sent[key] = now
            repeats = self._suppressed.pop(key, 0)
            dropped, self._dropped = self._dropped, 0
            self._send(self._format(record, headline, is_error, repeats, dropped))
        except Exception as exc:  # alert nie może przerwać programu ani wywołać kolejnego alertu
            _report_failure(exc)

    def _format(self, record: logging.LogRecord, headline: str, is_error: bool, repeats: int,
                dropped: int = 0) -> str:
        when = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M")
        lines = [f"🚨 BŁĄD: {headline}" if is_error else f"✅ {headline}", f"🖥️ {self.hostname} · {when}"]
        message = record.getMessage().strip()
        if message != headline:
            lines.append(f"📄 {record.name}: {_shorten(message)}")
        if record.exc_info and record.exc_info[1] is not None:
            error = record.exc_info[1]
            lines.append(f"⚠️ {type(error).__name__}: {_shorten(str(error))}")
        if repeats:
            lines.append(f"🔁 powtórzyło się {repeats}× od poprzedniego alertu")
        if dropped:
            lines.append(f"🔇 pominięto {dropped} innych alertów (limit {self.max_per_hour} na godzinę) – są w logu")
        text = redact_url("\n".join(lines))
        return text if len(text) <= TELEGRAM_LIMIT else text[: TELEGRAM_LIMIT - 1] + "…"


def telegram_sender(
    token: str,
    chat_id: str,
    *,
    session: requests.Session | Any | None = None,
    timeout: float = 15.0,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Sender:
    """Funkcja wysyłająca zwykły tekst (bez HTML) na czat admina.

    Błędy sieci i statusy 429/5xx są ponawiane z wykładniczym opóźnieniem (1 s, 2 s…).

    Raises (w zwróconej funkcji):
        RuntimeError: Telegram odrzucił wiadomość lub nie odpowiada.
    """
    http = session or requests.Session()
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(text: str) -> None:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        problem = "brak odpowiedzi"
        for attempt in range(attempts):
            if attempt:
                sleep(2 ** (attempt - 1))
            try:
                response = http.request("POST", url, json=payload, timeout=timeout)
            except requests.RequestException as exc:
                problem = str(exc)
                continue
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code == 200 and body.get("ok"):
                return
            problem = f"HTTP {response.status_code} {body.get('description', '')}".strip()
            if response.status_code not in RETRYABLE_STATUS:
                break
        raise RuntimeError(f"Telegram nie przyjął alertu: {redact_url(problem)}")

    return send


@dataclass
class AdminAlerts:
    """Zainstalowany kanał alertów; :meth:`stop` wysyła zaległe alerty i odłącza handler."""

    handler: TelegramLogHandler
    worker: _BackgroundSender

    def stop(self) -> None:
        logging.getLogger().removeHandler(self.handler)
        self.worker.stop()


def install_admin_alerts(
    token: str,
    chat_id: str,
    *,
    send: Sender | None = None,
    hostname: str | None = None,
) -> AdminAlerts | None:
    """Podłącza alerty admina do głównego loggera; bez tokenu lub czatu admina nic nie robi."""
    if not token or not chat_id:
        return None
    worker = _BackgroundSender(send or telegram_sender(token, chat_id))
    handler = TelegramLogHandler(worker.submit, hostname=hostname)
    handler._gunb_tool_alerts = True  # type: ignore[attr-defined]
    logging.getLogger().addHandler(handler)
    return AdminAlerts(handler, worker)


class _BackgroundSender:
    """Wysyła alerty w osobnym wątku, żeby awaria sieci nie blokowała bota."""

    def __init__(self, send: Sender, max_pending: int = 100) -> None:
        self._send = send
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=max_pending)
        self._thread = threading.Thread(target=self._run, name="alerty-admina", daemon=True)
        self._thread.start()

    def submit(self, text: str) -> None:
        try:
            self._queue.put_nowait(text)
        except queue.Full:  # zalew awarii – i tak są grupowane, nadmiar pomijamy
            pass

    def stop(self, timeout: float = 20.0) -> None:
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            return
        self._thread.join(timeout)

    def _run(self) -> None:
        while (text := self._queue.get()) is not None:
            try:
                self._send(text)
            except Exception as exc:
                _report_failure(exc)


def _http_headline(text: str, status: int | None) -> str:
    service = next((name for host, name in _SERVICES if host in text), "Serwer")
    if status is None:
        codes = _HTTP_STATUS_RE.findall(text)
        status = int(codes[-1]) if codes else None
    if service == "GUNB" and status in (403, 429):
        return f"GUNB zablokował dostęp (HTTP {status}) – możliwa blokada IP"
    if status:
        return f"{service} zwraca HTTP {status}"
    return f"{service} nie odpowiada"


def _exception_chain(error: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    while error is not None and id(error) not in seen and len(seen) < 10:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0][:200] if lines else "(pusty komunikat)"


def _shorten(text: str, limit: int = DETAIL_LIMIT) -> str:
    """Szczegóły do przeczytania na telefonie – pełna treść zostaje w logu."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _report_failure(exc: BaseException) -> None:
    print(f"Alert admina nie został wysłany: {redact_url(str(exc))}", file=sys.stderr)
