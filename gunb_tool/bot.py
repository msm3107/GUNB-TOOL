"""Interaktywny bot Telegram dla ekip budowlanych – każdy użytkownik ma własne filtry, tryb i listy.

Bot działa jako jeden stale uruchomiony proces (``python main.py --bot``) z dwoma wątkami:

* wątek główny odbiera wiadomości i kliknięcia przycisków (long polling ``getUpdates``),
* wątek zadań (:class:`JobsWorker`, własne połączenie z bazą i klienci HTTP) o ustalonych godzinach
  czasu polskiego pobiera dane GUNB (``bot.fetch_times``; nieudane pobieranie ponawia co godzinę),
  wysyła raporty „🌅 rano” / „🌙 wieczorem” i przypomnienia „⏰ Kiedy dzwonić”, a co
  ``instant_every_minutes`` minut – alerty 👀 watchlisty i tryb „⚡ od razu”.

Raporty i przypomnienia idą przez kolejkę ``wysylki``: zadanie najpierw zapisuje listę odbiorców, potem
każda osoba jest obsługiwana osobno – błąd jednej nie zatrzymuje innych, nieudana próba jest ponawiana
(1, 5, 15, 30 min; najwyżej 5 prób), a restart programu nie gubi ani nie dubluje obsłużonych odbiorców.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, time as clock_time, timedelta, timezone
from typing import Any, Callable, Sequence

from . import bot_ui as ui
from .bot_store import (SETUP_DONE, SETUP_STEPS, BotStore, BotUser, Send, UserFilters, investor_key,
                        watch_match)
from .bot_ui import BOT_COMMANDS, MENU_BUTTONS
from .clock import WARSAW, at_local_time, local
from .config import BotConfig
from .exporter import TELEGRAM_LIMIT, MessageFormatter, escape_html
from .gunb_scraper import GunbFormatError
from .http_client import HttpError
from .models import Investment
from .pipeline import IMPORT_LEASE, EmptyImport, ImportSkipped
from .scoring import HOT
from .stages import LONGEST_WINDOW_DAYS, get_trade, is_due
from .storage import LeadRepository
from .telegram_api import TelegramApiError

log = logging.getLogger(__name__)

__all__ = ["LeadBot", "JobsWorker", "MENU_BUTTONS", "BOT_COMMANDS"]

_PRIORITY_RANK = {"hot": 0, "normal": 1, "low": 2}

FETCH_RETRY_JOB = "pobieranie_ponow"
"""Termin ponowienia nieudanego pobierania (tabela ``zadania``; brak terminu = nic do ponowienia)."""
FETCH_RETRY_AFTER = timedelta(hours=1)
STAGE_REMINDER_JOB = "przypomnienia_etap"
IMPORT_JOB = "import"
"""Stan ostatniego importu GUNB (trwa / ok / blad / pominieto) – podgląd admina ``/status``."""
LAST_IMPORT_JOB = "import_udany"
"""Kiedy ostatni import GUNB zakończył się w całości."""
HEARTBEAT_JOB = "watek_zadan"
"""Ostatni cykl wątku zadań – po nim widać, że zadania w tle żyją."""
RECEIVE_HEARTBEAT_JOB = "petla_odbioru"
"""Ostatni udany odbiór aktualizacji z Telegrama – brak świeżego znaczy: bot głuchy (kontrola zdrowia)."""
REPORT_JOBS: dict[str, str] = {"raport_rano": "rano", "raport_wieczor": "wieczor"}
"""Zadanie raportu → tryb użytkowników, którzy go dostają."""
CLEANUP_JOB, CLEANUP_TIME = "porzadki", "03:30"
SENDS_KEPT = timedelta(days=35)
"""Zakończone wysyłki starsze niż tyle są usuwane (``/status`` pokazuje ostatnią dobę, ``/raport`` – z ``deliveries``)."""

SEND_BACKOFF: tuple[timedelta, ...] = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15),
                                       timedelta(minutes=30))
"""Przerwy przed kolejnymi próbami wysyłki (po 1., 2., 3. i 4. nieudanej)."""
MAX_SEND_ATTEMPTS = len(SEND_BACKOFF) + 1
STUCK_SEND_AFTER = timedelta(minutes=5)
"""Próba „w toku” dłużej niż tyle = proces padł w trakcie; wysyłka wraca do kolejki (może się powtórzyć)."""
QUIET_FROM, QUIET_UNTIL = clock_time(22, 0), clock_time(6, 0)
"""Cisza nocna (czas polski): spóźnione raporty i ponowienia nie wychodzą w nocy."""
LATE_GRACE = timedelta(minutes=15)
"""Tyle po terminie wysyłka jest jeszcze „na czas” – także gdy termin wypada w ciszy nocnej."""

NONE, SETUP, FULL = 0, 1, 2
"""Poziomy uprawnień: konto i pomoc (każdy) · ustawienia (test dozwolony, jeszcze nie ruszył) · inwestycje."""
TRIAL_LENGTH = timedelta(days=ui.TRIAL_DAYS)
ACCESS_REMINDER_BEFORE = timedelta(hours=24)
ACCESS_REMINDER_JOB = "dostep_przypomnienie"
ACCESS_END_JOB = "dostep_koniec"
ACCESS_JOBS = (ACCESS_REMINDER_JOB, ACCESS_END_JOB)
NO_ACCESS_TOAST = "⛔ Brak aktywnego dostępu (test albo abonament) – szczegóły: /konto"
SETUP_DONE_TOAST = "✅ To już ustawione – zmienisz w ⚙️ Ustawienia"
DB_BUSY_RETRIES = 3
"""Ile razy obsłużyć ponownie aktualizację, przy której baza była chwilowo zajęta."""
CONFLICT_JOB = "konflikt_telegrama"
CONFLICT_PAUSE = timedelta(minutes=10)
"""Tyle po ostatnim konflikcie 409 wątek zadań nic nie wysyła (drugi bot z tym samym tokenem)."""
WATCH_DIGEST_AFTER = 3
"""Więcej alertów obserwowanych naraz (np. po wznowieniu powiadomień) idzie jedną wiadomością."""

MENU_ACTIONS: dict[str, str] = {
    "📊 Inwestycje": "_show_news",
    "⭐ Zapisane": "_show_saved",
    "⚙️ Ustawienia": "_show_settings",
    "❓ Pomoc": "_show_help",
    # przyciski poprzedniego menu (ui.LEGACY_MENU_BUTTONS) – działają dalej
    "📊 Co nowego?": "_show_news",
    "🔎 Filtry": "_show_filters",
    "👀 Obserwowane": "_show_watchlist",
    "⏰ Kiedy wysyłać": "_show_mode",
    "🔥 Tylko HOT": "_toggle_hot",
    "📍 Blisko mnie": "_show_nearby",
    ui.CANCEL_BUTTON: "_cancel_input",
}
COMMAND_ACTIONS: dict[str, str] = {
    "/nowe": "_show_news",
    "/filtry": "_show_filters",
    "/blisko": "_show_nearby",
    "/branza": "_show_trade",
    "/zapisane": "_show_saved",
    "/obserwowane": "_show_watchlist",
    "/tryb": "_show_mode",
    "/tylkohot": "_toggle_hot",
    "/pomoc": "_show_help",
    "/konto": "_show_account",
    "/ustawienia": "_show_settings",
    "/uzytkownicy": "_show_users",
}
ACTION_LEVELS: dict[str, int] = {
    "_show_news": FULL, "_show_saved": FULL, "_show_watchlist": FULL,
    "_show_filters": SETUP, "_show_nearby": SETUP, "_show_trade": SETUP, "_show_mode": SETUP, "_toggle_hot": SETUP,
    "_cancel_input": NONE, "_show_help": NONE, "_show_account": NONE, "_show_users": NONE, "_show_settings": NONE,
}
"""Jaki poziom uprawnień jest potrzebny do ekranu z menu albo komendy (sprawdzane w jednym miejscu)."""
ADMIN_COMMANDS: dict[str, str] = {
    "/aktywuj": "_cmd_activate",  # /aktywuj <chat_id> <dni|data> – abonament
    "/przedluz": "_cmd_activate",  # to samo: dni liczone od końca obecnego dostępu
    "/odbierz": "_cmd_revoke",  # /odbierz <chat_id> – wyłącza dostęp od razu
    "/trial": "_cmd_trial",  # /trial <chat_id> – pozwala na 7-dniowy test (startuje klient)
    "/nowymodel": "_cmd_new_model",  # /nowymodel <chat_id|wszyscy> <dni|data> – termin dla dotychczasowych
    "/status": "_cmd_status",  # import GUNB, wątek zadań, wysyłki z ostatniej doby
    "/raport": "_cmd_pilot_report",  # /raport [7|30] – pilotaż: unikalne osoby i inwestycje
}
"""Komendy zastrzeżone dla ``bot.admins`` (``ADMIN_CHAT_ID``); u innych działają jak nieznany tekst."""


class LeadBot:
    """Logika bota: rozmowa z użytkownikami, doręczanie leadów, harmonogram.

    Args:
        repo: repozytorium leadów (wspólna baza SQLite).
        api: klient Bot API (``TelegramApi`` lub atrapa w testach).
        settings: sekcja ``bot`` konfiguracji.
        powiat_codes: monitorowane powiaty – do wyboru w filtrach.
        formatter: formater kart leadów.
        digest_threshold: tryb „od razu”: powyżej tylu leadów zamiast serii wiadomości idzie raport.
        max_age_days: starsze zmiany nie są doręczane.
        fetcher: funkcja pobierająca dane GUNB (wywoływana o ``fetch_times``). Zwraca opis wyniku
            (``str``) albo ``True``/``None`` przy sukcesie; ``False`` lub wyjątek = nieudane pobieranie
            (bot ponowi je za godzinę), :class:`ImportSkipped` = import się nie odbył (np. trwa inny).
        clock: bieżący czas UTC (domyślnie zegar bazy); godziny harmonogramu liczone są w czasie polskim.
    """

    def __init__(
        self,
        repo: LeadRepository,
        api: Any,
        *,
        settings: BotConfig,
        powiat_codes: Sequence[str],
        formatter: MessageFormatter,
        digest_threshold: int = 10,
        max_age_days: int = 14,
        fetcher: Callable[[], bool | str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.api = api
        self.settings = settings
        self.store = BotStore(repo)
        self.powiat_codes = tuple(powiat_codes)
        self.formatter = formatter
        self.digest_threshold = digest_threshold
        self.max_age_days = max_age_days
        self.fetcher = fetcher
        self._now = clock or repo.now
        self._offset: int | None = None
        self._db_busy: dict[int, int] = {}
        self._paused_by_conflict: datetime | None = None
        self.should_stop: Callable[[], bool] = lambda: False
        """Prośba o zakończenie (ustawia :class:`JobsWorker`) – długie pętle wysyłek kończą się po bieżącej osobie."""

    # === Pętla główna ==========================================================================

    def setup(self) -> None:
        """Rejestruje menu komend widoczne pod „/” w Telegramie."""
        self.api.set_my_commands(BOT_COMMANDS)

    def run_forever(self, *, should_stop: Callable[[], bool] = lambda: False,
                    sleep: Callable[[float], None] = time.sleep) -> None:
        """Odbieranie wiadomości i kliknięć; zadania w tle wykonuje osobny wątek (:class:`JobsWorker`).

        Kończy się po bieżącym long pollingu, gdy ``should_stop()`` zwróci ``True``.
        """
        self.setup()
        log.info("Bot uruchomiony – czekam na wiadomości (Ctrl+C kończy)")
        while not should_stop():
            try:
                self.poll_once(self.settings.poll_timeout)
                self.store.set_job_time(RECEIVE_HEARTBEAT_JOB, self._now())
            except TelegramApiError as exc:
                if exc.code == 409:  # drugi proces odbiera aktualizacje tym samym tokenem
                    self._note_conflict(exc)
                    sleep(30)
                else:
                    log.warning("Telegram: %s", exc)
                    sleep(5)
            except sqlite3.Error as exc:  # np. baza chwilowo zajęta przez VACUUM w wątku zadań
                log.warning("Baza chwilowo niedostępna: %s – ponawiam", exc)
                sleep(1)
            except Exception:  # pętla odbierania nie może paść – bez niej bot jest głuchy
                log.exception("Nieoczekiwany błąd pętli odbierania wiadomości")
                sleep(5)

    def _note_conflict(self, exc: TelegramApiError) -> None:
        """Telegram 409: ten sam token odbiera inny proces (np. zapomniany bot na drugim komputerze).

        Zapis w bazie wstrzymuje wysyłki wątku zadań na ``CONFLICT_PAUSE`` – dwa boty wysłałyby każdy
        raport dwa razy. Alert do admina idzie przy pierwszym konflikcie w oknie, potem tylko log.
        """
        now = self._now()
        try:
            previous = self.store.job_time(CONFLICT_JOB)
            self.store.set_job_time(CONFLICT_JOB, now)
        except sqlite3.Error:
            previous = None
        if previous is None or now - previous >= CONFLICT_PAUSE:
            log.error("Telegram 409: ten sam token odbiera inny proces bota – wstrzymuję wysyłki. "
                      "Zostaw włączonego tylko jednego bota.")
        else:
            log.warning("Telegram 409 (konflikt trwa): %s", exc.description)

    def poll_once(self, timeout: int) -> int:
        """Pobiera i obsługuje oczekujące aktualizacje; zwraca ich liczbę.

        Aktualizacja, której nie dało się obsłużyć przez chwilowo zajętą bazę, nie przesuwa offsetu –
        Telegram odda ją przy kolejnym pobraniu (najwyżej ``DB_BUSY_RETRIES`` razy, potem jest pomijana).
        """
        if self._offset is None:
            stored = self.store.job_last_run("telegram_offset")
            self._offset = int(stored) if stored else None
        updates = self.api.get_updates(self._offset, timeout)
        for update in updates:
            update_id = int(update["update_id"])
            try:
                self.handle_update(update)
            except sqlite3.OperationalError as exc:
                attempts = self._db_busy.get(update_id, 0) + 1
                self._db_busy[update_id] = attempts
                if attempts <= DB_BUSY_RETRIES:
                    log.warning("Aktualizacja %s: baza zajęta (%s) – obsłużę ją ponownie", update_id, exc)
                    break
                log.exception("Aktualizacja %s: baza zajęta %d razy – pomijam", update_id, attempts)
            except Exception:  # błąd jednej wiadomości nie może zatrzymać bota
                log.exception("Błąd obsługi aktualizacji %s", update_id)
            self._db_busy.pop(update_id, None)
            self._offset = update_id + 1
            try:
                self.store.mark_job("telegram_offset", str(self._offset))
            except sqlite3.Error as exc:  # offset zostaje w pamięci; zapisze się przy następnej aktualizacji
                log.warning("Nie zapisano offsetu Telegrama: %s", exc)
        return len(updates)

    def run_due_jobs(self) -> list[str]:
        """Jeden cykl wątku zadań: import GUNB, start raportów i przypomnień, kolejka wysyłek, „od razu”.

        Godziny z konfiguracji to czas polski (także po zmianie czasu), a znaczniki w bazie są w UTC.
        Zadanie, którego termin minął podczas przerwy w pracy bota, rusza po jego starcie – tego samego
        dnia i nie w ciszy nocnej (wtedy treść trafi do następnego raportu).
        """
        now = self._now()
        self.store.set_job_time(HEARTBEAT_JOB, now)
        ran: list[str] = []
        for hhmm in self.settings.fetch_times:
            name = f"pobieranie_{hhmm}"
            if self.fetcher is not None and self._due(name, now, hhmm):
                self.store.set_job_time(name, now)
                log.info("Harmonogram: pobieranie danych GUNB (%s)", hhmm)
                self._fetch()
                ran.append(name)
        retry_at = self.store.job_time(FETCH_RETRY_JOB)
        if self.fetcher is not None and retry_at is not None and retry_at <= now:
            log.info("Harmonogram: ponowienie nieudanego pobierania danych GUNB")
            self._fetch()
            ran.append(FETCH_RETRY_JOB)
        now = self._now()  # import mógł trwać długo
        conflict = self.store.job_time(CONFLICT_JOB)
        if conflict is not None and now - conflict < CONFLICT_PAUSE:  # drugi bot z tym samym tokenem
            if self._paused_by_conflict != conflict:
                log.warning("Wysyłki wstrzymane do %s: inny proces odbiera wiadomości tym samym tokenem",
                            f"{local(conflict + CONFLICT_PAUSE):%H:%M}")
                self._paused_by_conflict = conflict
            return ran  # zadania zostają „do zrobienia” – ruszą, gdy konflikt minie
        jobs = [(name, hhmm) for name, hhmm in (("raport_rano", self.settings.morning_time),
                                                  ("raport_wieczor", self.settings.evening_time),
                                                  (STAGE_REMINDER_JOB, self.settings.morning_time))]
        for name, hhmm in jobs:
            if self._due(name, now, hhmm):
                self.store.set_job_time(name, now)
                if self._quiet(now) and now - at_local_time(now, hhmm) > LATE_GRACE:
                    log.info("Harmonogram: %s pominięty – bot wrócił w ciszy nocnej", name)
                else:
                    self._start_sends(name, now)
                ran.append(name)
        if self._due(CLEANUP_JOB, now, CLEANUP_TIME):  # raz dziennie: kolejka wysyłek nie rośnie bez końca
            self.store.set_job_time(CLEANUP_JOB, now)
            self.store.prune_sends(now - SENDS_KEPT)
        self.process_sends()
        self.deliver_personal_reminders()
        last = self.store.job_time("natychmiast")
        interval = timedelta(minutes=self.settings.instant_every_minutes)
        if ran or last is None or last + interval <= now:
            self.store.set_job_time("natychmiast", now)
            if self.settings.access != "open":
                self.notify_access_changes()  # co kilka minut – przypomnienie i koniec dostępu bez opóźnień
                self.process_sends()
            self.deliver_instant()
            ran.append("natychmiast")
        return ran

    # === Kolejka wysyłek (raporty i przypomnienia) ===============================================

    def _start_sends(self, job: str, now: datetime, *, manual: bool = False) -> None:
        """Start zadania: zapisuje po jednej wysyłce na odbiorcę.

        Zadanie z harmonogramu ma klucz dnia (``raport_rano:2026-09-29``) – drugi start tego samego dnia
        niczego nie dubluje. Runda ręczna (``manual``) dostaje klucz niepowtarzalny – każda jest osobna.
        """
        if job == STAGE_REMINDER_JOB:
            recipients = [u.chat_id for u in self._subscribers() if u.branza is not None]
        else:
            recipients = [u.chat_id for u in self._subscribers(tryb=REPORT_JOBS[job])]
        key = f"{_utc_iso(now)}#{uuid.uuid4().hex[:8]}" if manual else local(now).date().isoformat()
        self.store.enqueue_sends(f"{job}:{key}", recipients)
        log.info("Harmonogram: %s – odbiorców %d", job, len(recipients))

    def process_sends(self) -> int:
        """Obsługuje zaległe wysyłki z kolejki – każdą osobę osobno; zwraca liczbę wysłanych wiadomości."""
        self.store.requeue_stuck_sends(self._now() - STUCK_SEND_AFTER)
        sent = 0
        for item in self.store.due_sends():
            if self.should_stop():
                break
            self.store.set_job_time(HEARTBEAT_JOB, self._now())  # długa kolejka (limity Telegrama) to nie zawieszenie
            sent += self._process_send(item)
        return sent

    def _process_send(self, item: Send) -> int:
        job = item.zadanie.partition(":")[0]
        user = self.store.get_user(item.chat_id)
        obstacle = self._send_obstacle(item, job, user)
        if obstacle is not None:
            self.store.finish_send(item.zadanie, item.chat_id, "pominieto", blad=obstacle)
            return 0
        assert user is not None
        now = self._now()
        if job in ACCESS_JOBS and self._quiet(now):  # informacja o dostępie nie przepada – czeka do rana
            self.store.finish_send(item.zadanie, item.chat_id, "oczekuje", retry_at=self._quiet_end(now))
            return 0
        if not self.store.claim_send(item.zadanie, item.chat_id):
            return 0  # tę wysyłkę obsłużył w międzyczasie inny proces
        try:
            if job == STAGE_REMINDER_JOB:
                delivered = self.send_stage_reminder(user)
            elif job in ACCESS_JOBS:
                delivered = self._send_access_notice(user, ended=job == ACCESS_END_JOB)
            else:
                delivered = self.send_report(user)
        except TelegramApiError as exc:
            if exc.blocked:
                self._delivery_failed(user, exc)
                self.store.finish_send(item.zadanie, item.chat_id, "zablokowany", blad=str(exc))
            else:
                self._send_failed(item, exc)
            return 0
        except Exception as exc:  # błąd przy jednej osobie (np. nietypowe dane) nie zatrzymuje pozostałych
            log.exception("Wysyłka %s do %s: nieoczekiwany błąd", item.zadanie, item.chat_id)
            self._send_failed(item, exc)
            return 0
        self.store.finish_send(item.zadanie, item.chat_id, "wyslano" if delivered else "pusto")
        return int(delivered)

    def _send_obstacle(self, item: Send, job: str, user: BotUser | None) -> str | None:
        """Powód, by wysyłki już nie robić (``None`` – można wysyłać)."""
        if user is None or user.status != "aktywny":
            return f"odbiorca {user.status if user else 'nieznany'}"
        if job in ACCESS_JOBS:  # wiadomości o dostępie idą także bez dostępu i nie przedawniają się
            if user.subscription_ends != item.zadanie.partition(":")[2]:
                return "termin dostępu się zmienił"
            if (job == ACCESS_END_JOB) == self._has_access(user):
                return "dostęp przedłużony" if job == ACCESS_END_JOB else "dostęp już się skończył"
            return None
        if not self._has_access(user):
            return "brak dostępu"
        if user.wstrzymane:
            return "powiadomienia wstrzymane"
        if job in REPORT_JOBS and user.tryb != REPORT_JOBS[job]:
            return "zmieniony tryb raportów"
        now = self._now()
        created = datetime.fromisoformat(item.utworzono)
        if local(created).date() != local(now).date():
            return "po czasie (inny dzień)"
        if self._quiet(now) and now - created > LATE_GRACE:
            return "cisza nocna"
        return None

    def _send_failed(self, item: Send, exc: Exception) -> None:
        """Nieudana próba: ponowienie po przerwie albo – po ostatniej próbie – błąd widoczny dla admina.

        Timeout nie mówi, czy Telegram przyjął wiadomość; ponowienie może ją więc powtórzyć (co najwyżej
        tyle razy, ile jest prób). Wolimy to niż ryzyko, że klient nie dostanie raportu wcale.
        """
        attempt = item.proby + 1
        if attempt >= MAX_SEND_ATTEMPTS:
            log.error("Wysyłka %s do %s nieudana po %d próbach: %s", item.zadanie, item.chat_id, attempt, exc)
            self.store.finish_send(item.zadanie, item.chat_id, "blad", blad=str(exc))
            return
        retry_at = self._now() + SEND_BACKOFF[attempt - 1]
        log.warning("Wysyłka %s do %s nieudana (próba %d/%d): %s – ponowię o %s", item.zadanie, item.chat_id,
                    attempt, MAX_SEND_ATTEMPTS, exc, f"{local(retry_at):%H:%M}")
        self.store.finish_send(item.zadanie, item.chat_id, "oczekuje", blad=str(exc), retry_at=retry_at)

    def _quiet(self, now: datetime) -> bool:
        """Czy w Polsce trwa cisza nocna (22:00–6:00)."""
        moment = local(now).time()
        return moment >= QUIET_FROM or moment < QUIET_UNTIL

    def _quiet_end(self, now: datetime) -> datetime:
        """Najbliższy koniec ciszy nocnej (6:00 czasu polskiego)."""
        morning = at_local_time(now, f"{QUIET_UNTIL:%H:%M}")
        return morning if morning > now else at_local_time(now + timedelta(days=1), f"{QUIET_UNTIL:%H:%M}")

    # === Aktualizacje =========================================================================

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._on_callback(update["callback_query"])
        elif "message" in update:
            self._on_message(update["message"])

    def _on_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or chat.get("type") != "private":
            return  # bot obsługuje rozmowy prywatne
        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
        if command in ADMIN_COMMANDS and chat_id in self.settings.admins:  # działa też bez /start admina
            getattr(self, ADMIN_COMMANDS[command])(chat_id, text.split()[1:])
            return
        user = self.store.get_user(chat_id)
        if command == "/start" or user is None:
            self._start(chat_id, message.get("from") or {})
            return
        if user.status == "zablokowany":  # odblokował bota i znów pisze – dostęp zostaje, jaki był
            self.store.set_status(chat_id, "aktywny")
            user = self.store.get_user(chat_id)
        if user.status == "odrzucony":
            self._send(chat_id, ui.rejected_text())
            return
        action = COMMAND_ACTIONS.get(command) if command else MENU_ACTIONS.get(text)
        needed = ACTION_LEVELS.get(action, FULL) if action and not message.get("location") else SETUP
        if self._level(user) < needed:  # bramkarz: jedno miejsce dla wszystkich wiadomości i komend
            self._send_gate(user)
            return
        if message.get("location"):
            self._set_base(user, message["location"])
            return
        if action:
            if user.oczekuje_na:
                self.store.set_awaiting(chat_id, None)
                user = self.store.get_user(chat_id)
            getattr(self, action)(user)
        elif user.oczekuje_na and text:
            self._text_input(user, text)
        else:
            self._send(chat_id, ui.unknown_text(), ui.menu_keyboard())

    def _on_callback(self, callback: dict[str, Any]) -> None:
        """Kliknięcie przycisku: uprawnienia sprawdza się dla osoby, która kliknęła (``from.id``)."""
        message = callback.get("message") or {}
        sender = (callback.get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id", sender)
        message_id = message.get("message_id")
        data = callback.get("data") or ""
        answer: str | None = None
        try:
            if data.startswith("adm:"):
                answer = self._admin_decision(sender, chat_id, message_id, data)
            elif sender is None or chat_id != sender:
                answer = "⛔ Ten przycisk nie jest dla Ciebie"
            else:
                user = self.store.get_user(sender)
                prefix, _, arg = data.partition(":")
                handler = _CALLBACKS.get(prefix)
                if user is None or user.status == "odrzucony":
                    answer = NO_ACCESS_TOAST
                elif self._level(user) < _CALLBACK_LEVELS.get(prefix, FULL):
                    answer = "▶️ Najpierw zacznij 7-dniowy test – /konto" if user.trial_available else NO_ACCESS_TOAST
                else:
                    answer = handler(self, user, arg, message_id) if handler else None
        finally:
            self.api.answer_callback_query(callback.get("id"), answer)

    # === Rejestracja i admin ===================================================================

    def _start(self, chat_id: int, sender: dict[str, Any]) -> None:
        """Rejestracja: nowa osoba zapisuje się bez dostępu, admin dostaje jej kartę.

        Ponowny ``/start`` niczego nie odnawia – ani testu, ani abonamentu.
        """
        existed = self.store.get_user(chat_id) is not None
        user = self.store.register(chat_id, sender.get("first_name"), sender.get("username"), status="aktywny",
                                   backlog_days=self.settings.welcome_backlog_days)
        if user.status in ("zablokowany", "oczekuje"):
            self.store.set_status(chat_id, "aktywny")
            user = self.store.get_user(chat_id)
        if user.status == "odrzucony":
            self._send(chat_id, ui.rejected_text())
            return
        level = self._level(user)
        if level == NONE:
            self._send(chat_id, self._gate_text(user))
        else:
            self._send(chat_id, ui.welcome_text(user.imie), ui.menu_keyboard())
            if not user.setup_done:  # pierwsze kroki – także po przerwie wracamy do tego samego pytania
                self._show_setup_step(user)
            elif level == SETUP:
                self._send(chat_id, *ui.trial_offer())
        if not existed and level < FULL:
            text, markup = ui.new_user_card(user)
            for admin in self.settings.admins:
                self._send_safely(admin, text, markup)

    def _admin_decision(self, sender: int | None, chat_id: int, message_id: int, data: str) -> str:
        """Przyciski z karty nowej osoby: 🎁 test 7 dni, ✅ 30 dni, ⛔ odrzuć – tylko dla admina, który kliknął."""
        if sender not in self.settings.admins:
            return "⛔ Tylko administrator może to zrobić"
        _, decision, target = data.split(":", 2)
        user = self.store.get_user(int(target)) if target.lstrip("-").isdigit() else None
        if user is None:
            return "Nie ma takiej osoby"
        if decision == "trial":
            self.api.edit_message_text(chat_id, message_id, self._allow_trial(user))
            return "🎁 Test dostępny"
        if decision == "ok":
            self.api.edit_message_text(chat_id, message_id, self._grant_paid(user, days=ui.DEFAULT_PAID_DAYS))
            return "✅ Abonament aktywny"
        self.store.set_status(user.chat_id, "odrzucony")
        self.store.revoke_access(user.chat_id)
        self._send_safely(user.chat_id, ui.rejected_text())
        self.api.edit_message_text(chat_id, message_id, f"⛔ Odrzucono: {escape_html(user.display_name)}")
        return "⛔ Odrzucono"

    # === Dostęp: test i abonament ====================================================================

    def _cmd_activate(self, admin_chat: int, args: list[str]) -> None:
        """``/aktywuj`` i ``/przedluz <chat_id> <dni|RRRR-MM-DD|DD.MM.RRRR>`` – tylko admin."""
        term = self._parse_term(args[1]) if len(args) == 2 and _is_chat_id(args[0]) else None
        if term is None:
            self._send(admin_chat, ui.admin_usage_text())
            return
        user = self._known_user(admin_chat, int(args[0]))
        if user is not None:
            days, until = term
            self._send(admin_chat, self._grant_paid(user, days=days, until=until))

    def _cmd_trial(self, admin_chat: int, args: list[str]) -> None:
        """``/trial <chat_id>`` – tylko admin: pozwala na 7-dniowy test; zegar rusza, gdy klient kliknie start."""
        if len(args) != 1 or not _is_chat_id(args[0]):
            self._send(admin_chat, ui.admin_usage_text())
            return
        user = self._known_user(admin_chat, int(args[0]))
        if user is not None:
            self._send(admin_chat, self._allow_trial(user))

    def _cmd_revoke(self, admin_chat: int, args: list[str]) -> None:
        """``/odbierz <chat_id>`` – tylko admin: wyłącza dostęp od razu (dane klienta zostają)."""
        if len(args) != 1 or not _is_chat_id(args[0]):
            self._send(admin_chat, ui.admin_usage_text())
            return
        user = self._known_user(admin_chat, int(args[0]))
        if user is not None:
            self.store.revoke_access(user.chat_id)
            self._notify(user.chat_id, ui.access_revoked_text(self._contact_html()))
            self._send(admin_chat, ui.admin_revoked_text(user))

    def _cmd_new_model(self, admin_chat: int, args: list[str]) -> None:
        """``/nowymodel <chat_id|wszyscy> <dni|data>`` – dotychczasowym użytkownikom (dostęp bez terminu)
        admin świadomie ustawia termin; potem obowiązują ich zwykłe zasady (przypomnienie, koniec, przedłużenie)."""
        term = self._parse_term(args[1]) if len(args) == 2 else None
        if term is None or not (args[0].lower() == "wszyscy" or _is_chat_id(args[0])):
            self._send(admin_chat, ui.admin_usage_text())
            return
        targets = [u for u in self.store.unlimited_users() if args[0].lower() == "wszyscy" or str(u.chat_id) == args[0]]
        days, until = term
        ends_iso = _utc_iso(until or self._now() + timedelta(days=days or 0))
        ends_on = self._local_date(ends_iso, with_time=True)
        for user in targets:
            self.store.set_access(user.chat_id, ends_iso)
            self.store.record_event(user.chat_id, "dostep_przedluzony")
            self._notify(user.chat_id, ui.access_term_text(ends_on))
        self._send(admin_chat, ui.admin_new_model_text(len(targets), ends_on))

    def _known_user(self, admin_chat: int, chat_id: int) -> BotUser | None:
        user = self.store.get_user(chat_id)
        if user is None:
            self._send(admin_chat, ui.admin_unknown_user_text(chat_id))
        return user

    def _parse_term(self, text: str) -> tuple[int | None, datetime | None] | None:
        """Termin od admina: liczba dni (1–3650) albo data (koniec dnia czasu polskiego); ``None`` – błędny."""
        if text.isdigit():
            return (int(text), None) if 1 <= int(text) <= 3650 else None
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                day = datetime.strptime(text, fmt).date()
            except ValueError:
                continue
            until = datetime.combine(day, clock_time(23, 59), tzinfo=WARSAW).astimezone(timezone.utc)
            return (None, until) if until > self._now() else None
        return None

    def _grant_paid(self, user: BotUser, *, days: int | None = None, until: datetime | None = None) -> str:
        """Abonament do daty albo na N dni – liczone od końca trwającego dostępu (klient nie traci dni).

        Zastępuje dostęp bez limitu (przełączenie na nowy model); zwraca potwierdzenie dla admina.
        """
        now = self._now()
        if until is None:
            current = datetime.fromisoformat(user.subscription_ends) \
                if user.subscription_ends and user.has_subscription(_utc_iso(now)) else now
            until = current + timedelta(days=days or 0)
        ends_iso = _utc_iso(until)
        self.store.set_access(user.chat_id, ends_iso)
        self.store.record_event(user.chat_id, "dostep_przedluzony")
        if user.status != "aktywny":  # np. wcześniej odrzucony albo „zablokowany” – admin daje nową szansę
            self.store.set_status(user.chat_id, "aktywny")
        ends_on = self._local_date(ends_iso, with_time=True)
        delivered = self._notify(user.chat_id, ui.activated_text(ends_on, user.imie, days=days), ui.menu_keyboard())
        if delivered and not user.setup_done:
            self._safely(lambda: self._show_setup_step(user))
        return ui.admin_granted_text(user, ends_on, days=days, delivered=delivered)

    def _allow_trial(self, user: BotUser) -> str:
        """Pozwolenie na test (raz na osobę); zwraca odpowiedź dla admina."""
        if user.trial_used:
            return ui.admin_trial_used_text(user, self._local_date(user.test_koniec, with_time=True))
        if self._has_access(user):
            ends_on = self._local_date(user.subscription_ends) if user.has_subscription(_utc_iso(self._now())) \
                else "bez terminu"
            return ui.trial_skipped_text(user, ends_on)
        self.store.allow_trial(user.chat_id)
        if user.status != "aktywny":
            self.store.set_status(user.chat_id, "aktywny")
        delivered = self._notify(user.chat_id, *ui.trial_offer(setup_done=user.setup_done))
        if delivered and not user.setup_done:  # najpierw branża i obszar, start testu na końcu
            self._safely(lambda: self._show_setup_step(user))
        return ui.admin_trial_allowed_text(user, delivered=delivered)

    def _cb_trial_start(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """„▶️ Zacznij 7-dniowy test” – świadomy start po ustawieniach; kolejne kliknięcia nic nie zmieniają."""
        if user.trial_used:
            ends_on = self._local_date(user.test_koniec, with_time=True)
            if user.on_trial and self._has_access(user):
                return f"🎁 Test trwa do {ends_on}"
            return f"Darmowy test został już wykorzystany (do {ends_on})"
        if self._has_access(user):
            return "✅ Masz już pełny dostęp"
        if not user.test_dozwolony:
            return "⛔ Test nie jest jeszcze dostępny – czekamy na administratora"
        if not user.setup_done:
            self._show_setup_step(user)
            return "⚙️ Najpierw dwa krótkie pytania – potem start testu"
        now = self._now()
        if not self.store.start_trial(user.chat_id, now, now + TRIAL_LENGTH):
            return None  # drugie kliknięcie w tej samej chwili – test już ruszył
        self.store.record_event(user.chat_id, "test_start")
        ends_on = self._local_date(_utc_iso(now + TRIAL_LENGTH), with_time=True)
        self._send(user.chat_id, ui.trial_started_text(ends_on), ui.menu_keyboard())
        self._send(user.chat_id, *self._history_screen(self.store.get_user(user.chat_id) or user, 0,
                                                       head=ui.first_review_head()))
        return "🎁 Test wystartował"

    # === Pierwsze kroki: branża → obszar → gotowe ========================================================

    def _show_setup_step(self, user: BotUser, message_id: int | None = None) -> None:
        """Bieżący krok pierwszej konfiguracji (nowa wiadomość albo podmiana ``message_id``)."""
        step = user.konfiguracja if user.konfiguracja in SETUP_STEPS else "branza"
        if user.konfiguracja != step:
            self.store.set_setup_step(user.chat_id, step)
        screen = ui.setup_trade_step() if step == "branza" else \
            ui.setup_area_step(self.store.place_options(self.powiat_codes), self._region_label())
        if message_id is None:
            self._send(user.chat_id, *screen)
        else:
            self.api.edit_message_text(user.chat_id, message_id, *screen)

    def _cb_setup_trade(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """Krok 1/2 – branża („ob:<branża>”, „ob:none”); „ob:resume” wraca do przerwanego kroku."""
        if user.setup_done:  # stary przycisk z historii czatu – pierwsze kroki są za nim
            return SETUP_DONE_TOAST
        if arg == "resume":
            self._show_setup_step(user)
            return None
        trade = get_trade(arg)
        if trade is None and arg != "none":
            return None
        self.store.set_trade(user.chat_id, trade.key if trade else None)
        self.store.set_setup_step(user.chat_id, "obszar")
        self._show_setup_step(self.store.get_user(user.chat_id) or user, message_id)
        return f"🧰 {trade.label}" if trade else "🏗️ Wszystkie etapy"

    def _cb_setup_area(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """Krok 2/2 – obszar: powiat („oa:p:<kod>”), cały obszar, pinezka bazy albo wpisana miejscowość."""
        if user.setup_done:  # stary przycisk – filtrów nie nadpisujemy
            return SETUP_DONE_TOAST
        if arg == "loc":
            self._send(user.chat_id, *ui.location_request())
            return "👇 Wyślij pinezkę przyciskiem na dole ekranu"
        if arg == "txt":
            return self._ask(user, "miejsce")
        if arg == "all":
            filters = replace(user.filtry, powiaty=(), miejsca=(), promien_km=None)
        elif arg.startswith("p:") and arg[2:] in self._place_names():  # te same powiaty, co na przyciskach
            filters = replace(user.filtry, powiaty=(arg[2:],), miejsca=(), promien_km=None)
        else:
            return None
        self.store.set_filters(user.chat_id, filters)
        self._finish_setup(user, message_id)
        return "📍 Zapisano obszar"

    def _finish_setup(self, user: BotUser, message_id: int | None = None) -> None:
        """Koniec pierwszej konfiguracji: podsumowanie; z dostępem od razu przegląd ostatnich 30 dni."""
        self.store.set_setup_step(user.chat_id, SETUP_DONE)
        self.store.record_event(user.chat_id, "konfiguracja")
        user = self.store.get_user(user.chat_id) or user
        level = self._level(user)
        text, markup = ui.setup_summary(user, ui.place_label(user.filtry, self._place_names()), self.settings,
                                        can_start_trial=level == SETUP)
        if message_id is None:
            self._send(user.chat_id, text, markup or ui.menu_keyboard())
        else:
            self.api.edit_message_text(user.chat_id, message_id, text, markup)
        if level == FULL:
            self._send(user.chat_id, *self._history_screen(user, 0, head=ui.first_review_head()))

    def _region_label(self) -> str:
        """Monitorowany obszar słowami (np. „Olsztyn, powiat olsztyński”)."""
        names = [label for _, label in self.store.place_options(self.powiat_codes)]
        return ", ".join(names) if names else "cały monitorowany obszar"

    def _safely(self, action: Callable[[], None]) -> None:
        try:
            action()
        except TelegramApiError as exc:
            log.warning("Nie udało się wysłać wiadomości: %s", exc)

    def notify_access_changes(self) -> None:
        """Jedno przypomnienie przed końcem testu/abonamentu i jedna informacja po nim (przez kolejkę wysyłek).

        Termin, o którym poszła wiadomość, zapisuje się razem z wysyłką – restart niczego nie dubluje,
        a przedłużenie (nowy termin) zaczyna cykl od nowa. W ciszy nocnej wiadomości czekają do rana.
        """
        now = self._now()
        if self._quiet(now):
            return
        admins = self.settings.admins
        for user in self.store.access_ending(now, now + ACCESS_REMINDER_BEFORE, admins=admins):
            with self.repo.transaction():
                self.store.enqueue_sends(f"{ACCESS_REMINDER_JOB}:{user.subscription_ends}", [user.chat_id])
                self.store.mark_access_reminded(user.chat_id, user.subscription_ends or "")
        ended = self.store.access_ended(now, admins=admins)
        for user in ended:
            with self.repo.transaction():
                self.store.enqueue_sends(f"{ACCESS_END_JOB}:{user.subscription_ends}", [user.chat_id])
                self.store.mark_access_end_reported(user.chat_id, user.subscription_ends or "")
        if ended:
            entries = [(user, self._local_date(user.subscription_ends, with_time=True)) for user in ended]
            for admin in admins:
                self._send_safely(admin, ui.admin_expired_text(entries))

    def _send_access_notice(self, user: BotUser, *, ended: bool) -> bool:
        ends_on = self._local_date(user.subscription_ends, with_time=True)
        contact = self._contact_html()
        self._send(user.chat_id, ui.access_ended_text(ends_on, trial=user.on_trial, contact_html=contact) if ended
                   else ui.access_reminder_text(ends_on, trial=user.on_trial, contact_html=contact))
        return True

    def _level(self, user: BotUser) -> int:
        """Poziom uprawnień: pełny dostęp, same ustawienia (test czeka na start) albo tylko konto i pomoc."""
        if self._has_access(user):
            return FULL
        return SETUP if user.trial_available else NONE

    def _access_state(self, user: BotUser) -> tuple[str, str | None]:
        """Stan dostępu do ekranu „Konto”, bramki i listy admina: (stan, termin w czasie polskim)."""
        if user.chat_id in self.settings.admins:
            return "admin", None
        if self.settings.access == "open":
            return "open", None
        if user.bez_limitu:
            return "bez_limitu", None
        ends_on = self._local_date(user.subscription_ends, with_time=True) if user.subscription_ends else None
        if user.has_subscription(_utc_iso(self._now())):
            return ("test" if user.on_trial else "platny"), ends_on
        if user.trial_available:
            return "test_dostepny", None
        if user.subscription_ends and user.is_active:
            return ("test_koniec" if user.on_trial else "platny_koniec"), ends_on
        if user.subscription_ends:
            return "wylaczony", ends_on
        return "brak", None

    def _send_gate(self, user: BotUser) -> None:
        if user.trial_available:
            self._send(user.chat_id, *ui.trial_waiting())
        else:
            self._send(user.chat_id, self._gate_text(user))

    def _notify(self, chat_id: int, text: str, markup: dict | None = None) -> bool:
        """Wiadomość o zmianie dostępu – błąd wysyłki nie przerywa akcji admina; zwraca, czy doszła."""
        try:
            self._send(chat_id, text, markup)
        except TelegramApiError as exc:
            log.warning("Nie udało się powiadomić %s: %s", chat_id, exc)
            return False
        return True

    def _cmd_pilot_report(self, admin_chat: int, args: list[str]) -> None:
        """``/raport [dni]`` – tylko admin: wysłane (z ``deliveries``) i zdarzenia z ostatnich 7/30 dni."""
        days = int(args[0]) if args and args[0].isdigit() and 1 <= int(args[0]) <= 90 else 7
        since = self._now() - timedelta(days=days)
        self._send(admin_chat, ui.pilot_report(days, sent=self.store.delivery_counts(since),
                                               events=self.store.event_counts(since)))

    def _cmd_status(self, admin_chat: int, args: list[str]) -> None:
        """``/status`` – tylko admin: stan importu GUNB, wątku zadań i wysyłek z ostatniej doby."""
        now = self._now()
        day_ago = now - timedelta(hours=24)
        self._send(admin_chat, ui.status_text(
            now=now, import_status=self.store.job_status(IMPORT_JOB), last_import=self.store.job_time(LAST_IMPORT_JOB),
            retry_at=self.store.job_time(FETCH_RETRY_JOB), heartbeat=self.store.job_time(HEARTBEAT_JOB),
            sends=self.store.send_counts(day_ago), failed=self.store.failed_sends(day_ago),
        ))

    def _has_access(self, user: BotUser) -> bool:
        """Pełny dostęp – ta sama reguła co zapytanie ``BotStore.subscribers`` dla wysyłek w tle."""
        return (user.chat_id in self.settings.admins or self.settings.access == "open" or user.bez_limitu
                or user.has_subscription(_utc_iso(self._now())))

    def _gate_text(self, user: BotUser) -> str:
        state, ends_on = self._access_state(user)
        contact = self._contact_html()
        if state in ("test_koniec", "platny_koniec"):
            return ui.access_ended_text(ends_on or "—", trial=state == "test_koniec", contact_html=contact)
        if state == "wylaczony":
            return ui.access_revoked_text(contact)
        return ui.gate_text(contact)

    def _contact_html(self) -> str:
        return ui.admin_contact_html(self.settings.admin_contact, self.settings.admins)

    def _subscribers(self, tryb: str | None = None) -> list[BotUser]:
        """Odbiorcy pętli wysyłkowych: z bazy tylko osoby z dostępem (i admini), bez tych z pauzą."""
        if self.settings.access == "open":
            return [user for user in self.store.users(tryb=tryb) if not user.wstrzymane]
        return self.store.subscribers(_utc_iso(self._now()), admins=self.settings.admins, tryb=tryb)

    def _local_date(self, utc_iso: str | None, *, with_time: bool = False) -> str:
        """Data z bazy (UTC) w czasie polskim – do komunikatów (np. „29.10.2026” albo „29.10.2026, 07:00”)."""
        if not utc_iso:
            return "—"
        return local(datetime.fromisoformat(utc_iso)).strftime("%d.%m.%Y, %H:%M" if with_time else "%d.%m.%Y")

    # === Ekrany z menu =========================================================================

    def _show_news(self, user: BotUser) -> None:
        self.send_report(user, on_demand=True)

    def _show_filters(self, user: BotUser, prefix: str = "") -> None:
        text, markup = ui.filters_screen(user, self._place_names(), self.settings, prefix)
        self._send(user.chat_id, text, markup)

    def _show_saved(self, user: BotUser, page: int = 0) -> None:
        text, markup = self._saved_page(user, page)
        self._send(user.chat_id, text, markup)

    def _show_watchlist(self, user: BotUser) -> None:
        text, markup = ui.watch_screen(self.store.watchlist(user.chat_id))
        self._send(user.chat_id, text, markup)

    def _show_mode(self, user: BotUser) -> None:
        text, markup = ui.mode_screen(user, self.settings)
        self._send(user.chat_id, text, markup)

    def _toggle_hot(self, user: BotUser) -> None:
        self.store.set_hot_only(user.chat_id, not user.tylko_hot)
        self._send(user.chat_id, ui.hot_only_text(not user.tylko_hot), ui.menu_keyboard())

    def _show_nearby(self, user: BotUser) -> None:
        if user.filtry.baza is None:
            self._send(user.chat_id, *ui.location_request())  # bez bazy od razu prośba o pinezkę
            return
        self._send(user.chat_id, *ui.nearby_screen(user.filtry))

    def _cancel_input(self, user: BotUser) -> None:
        self._send(user.chat_id, "👌 Bez zmian.", ui.menu_keyboard())

    def _show_trade(self, user: BotUser) -> None:
        self._send(user.chat_id, *ui.trade_picker(user.branza))

    def _set_base(self, user: BotUser, location: dict[str, Any]) -> None:
        """Pinezka z Telegrama = baza firmy; promień (domyślnie 15 km) od razu zastępuje powiaty i miejscowości."""
        try:
            base = (round(float(location["latitude"]), 5), round(float(location["longitude"]), 5))
        except (KeyError, TypeError, ValueError):
            self._send(user.chat_id, ui.unknown_text(), ui.menu_keyboard())
            return
        filters = replace(user.filtry, baza=base, promien_km=user.filtry.promien_km or ui.DEFAULT_RADIUS_KM,
                          powiaty=(), miejsca=())
        if user.oczekuje_na:  # pinezka zamiast wpisywanego tekstu – kończymy tamto pytanie
            self.store.set_awaiting(user.chat_id, None)
        self.store.set_filters(user.chat_id, filters)
        self._send(user.chat_id, ui.base_saved_text(filters), ui.menu_keyboard())
        nearest = self.store.nearest_investment_km(base)
        if nearest is None or nearest > (filters.promien_km or 0):  # baza spoza monitorowanego obszaru
            self._send(user.chat_id, ui.base_far_text(nearest, self._region_label()))
        if user.konfiguracja == "obszar":
            self._finish_setup(user)
        else:
            self._send(user.chat_id, *ui.nearby_screen(filters))

    def _show_help(self, user: BotUser) -> None:
        self._send(user.chat_id, ui.help_text(self.settings, admin=user.chat_id in self.settings.admins),
                   ui.menu_keyboard())

    def _show_account(self, user: BotUser) -> None:
        """„👤 Konto” – jaki dostęp, do kiedy i jak przedłużyć; działa także po końcu dostępu."""
        self._send(user.chat_id, *self._account_screen(user))

    def _account_screen(self, user: BotUser) -> tuple[str, dict | None]:
        state, ends_on = self._access_state(user)
        markup = None
        if state == "test_dostepny":
            markup = ui.inline([[ui.START_TRIAL_BUTTON if user.setup_done else ui.SETUP_RESUME_BUTTON]])
        return ui.account_text(state=state, ends_on=ends_on, contact_html=self._contact_html()), markup

    def _show_settings(self, user: BotUser) -> None:
        self._send(user.chat_id, *self._settings_screen(user))

    def _settings_screen(self, user: BotUser) -> tuple[str, dict]:
        return ui.settings_screen(user, place=ui.place_label(user.filtry, self._place_names()), settings=self.settings,
                                  watch_count=len(self.store.watchlist(user.chat_id)),
                                  account=self._subscription_label(user))

    def _cb_settings(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """Przyciski „⚙️ Ustawienia”: każdy ekran ma swój poziom uprawnień (konto – zawsze)."""
        screens: dict[str, tuple[int, Callable[[], tuple[str, dict | None]]]] = {
            "f": (SETUP, lambda: ui.filters_screen(user, self._place_names(), self.settings)),
            "b": (SETUP, lambda: ui.trade_picker(user.branza)),
            "m": (SETUP, lambda: ui.mode_screen(user, self.settings)),
            "w": (FULL, lambda: ui.watch_screen(self.store.watchlist(user.chat_id))),
            "k": (NONE, lambda: self._account_screen(user)),
            "0": (NONE, lambda: self._settings_screen(user)),
        }
        if arg == "p":  # ⏸️ / ▶️ – pauza wszystkich automatycznych wiadomości (dostęp biegnie dalej)
            self.store.set_paused(user.chat_id, not user.wstrzymane)
            user = self.store.get_user(user.chat_id) or user
            self.api.edit_message_text(user.chat_id, message_id, *self._settings_screen(user))
            self._send(user.chat_id, ui.pause_text(user.wstrzymane))
            return "⏸️ Wstrzymano" if user.wstrzymane else "▶️ Wznowiono"
        if arg not in screens:
            return None
        needed, screen = screens[arg]
        if self._level(user) < needed:
            return "▶️ Najpierw zacznij 7-dniowy test – /konto" if user.trial_available else NO_ACCESS_TOAST
        self.api.edit_message_text(user.chat_id, message_id, *screen())
        return None

    def _show_users(self, user: BotUser) -> None:
        if user.chat_id not in self.settings.admins:
            self._send(user.chat_id, ui.unknown_text(), ui.menu_keyboard())
            return
        everyone = [u for status in ("aktywny", "oczekuje", "zablokowany", "odrzucony") for u in self.store.users(status)]
        self._send(user.chat_id, ui.users_list(everyone, self._subscription_label))

    def _subscription_label(self, user: BotUser) -> str:
        """Dostęp na liście admina, np. „💳 do 29.10.2026”, „🎁 test do …”, „⌛ wygasł …”, „nieaktywny”."""
        state, ends_on = self._access_state(user)
        day = self._local_date(user.subscription_ends)
        return {
            "admin": "👑 admin", "open": "✅ otwarty", "bez_limitu": "♾️ bez terminu (dotychczasowy)",
            "test": f"🎁 test do {ends_on}", "platny": f"💳 do {day}", "test_dostepny": "🎁 test czeka na start",
            "test_koniec": f"⌛ test skończył się {day}", "platny_koniec": f"⌛ wygasł {day}",
            "wylaczony": "⛔ wyłączony",
        }.get(state, "nieaktywny")

    # === Kliknięcia: lead ======================================================================

    def _cb_open(self, user: BotUser, arg: str, message_id: int) -> str | None:
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono inwestycji"
        self._send_card(user, inv)
        self.store.record_event(user.chat_id, "szczegoly", inv.id_sprawy)
        return None

    def _cb_flag(self, user: BotUser, arg: str, message_id: int, *, toast: str, **flag: bool) -> str:
        """Ustawia jedno oznaczenie na wartość zapisaną w przycisku (idempotentnie) i odświeża przyciski."""
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono inwestycji"
        self.store.set_lead_flags(user.chat_id, inv.id_sprawy, **flag)
        self._refresh_keyboard(user, inv, message_id)
        return toast

    def _cb_save(self, user: BotUser, arg: str, message_id: int) -> str:
        """``s1:``/``s:`` (starsze przyciski) – zapisz; ponowione kliknięcie niczego nie cofa."""
        inv = self._lead(arg)
        if inv is not None and not self.store.lead_flags(user.chat_id, inv.id_sprawy).saved:
            self.store.record_event(user.chat_id, "zapis", inv.id_sprawy)
        return self._cb_flag(user, arg, message_id, saved=True, toast="⭐ Zapisano – znajdziesz je pod „⭐ Zapisane”")

    def _cb_unsave(self, user: BotUser, arg: str, message_id: int) -> str:
        return self._cb_flag(user, arg, message_id, saved=False, toast="Usunięto z zapisanych")

    def _cb_reviewed(self, user: BotUser, arg: str, message_id: int) -> str:
        """``r1:``/``r:`` – przejrzane; zapisanie zostaje."""
        return self._cb_flag(user, arg, message_id, reviewed=True, toast="✅ Oznaczono jako przejrzane")

    def _cb_unreviewed(self, user: BotUser, arg: str, message_id: int) -> str:
        return self._cb_flag(user, arg, message_id, reviewed=False, toast="Zdjęto oznaczenie „przejrzane”")

    def _cb_hide(self, user: BotUser, arg: str, message_id: int) -> str:
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono inwestycji"
        self.store.set_lead_flags(user.chat_id, inv.id_sprawy, hidden=True)
        text, markup = ui.hidden_card(inv)
        self.api.edit_message_text(user.chat_id, message_id, text, markup)
        return "🗑️ Ukryto"

    def _cb_unhide(self, user: BotUser, arg: str, message_id: int) -> str:
        """„↩️ Przywróć” – zdejmuje tylko ukrycie; zapisanie i przejrzenie wracają bez zmian."""
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono inwestycji"
        self.store.set_lead_flags(user.chat_id, inv.id_sprawy, hidden=False)
        text, markup = self._card(user, inv)
        self.api.edit_message_text(user.chat_id, message_id, text, markup)
        return "↩️ Przywrócono"

    def _cb_watch_investor(self, user: BotUser, arg: str, message_id: int) -> str:
        inv = self._lead(arg)
        key = investor_key(inv.inwestor) if inv else None
        if inv is None or key is None:
            return "Ta inwestycja nie ma jawnego inwestora"
        return self._toggle_watch(user, inv, "inwestor", key, inv.inwestor or key, message_id)

    def _cb_watch_gmina(self, user: BotUser, arg: str, message_id: int) -> str:
        inv = self._lead(arg)
        if inv is None or not inv.gmina_teryt:
            return "Brak gminy dla tej inwestycji"
        label = inv.gmina or inv.miejscowosc or inv.gmina_teryt
        return self._toggle_watch(user, inv, "gmina", inv.gmina_teryt, label, message_id)

    def _toggle_watch(self, user: BotUser, inv: Investment, kind: str, value: str, label: str, message_id: int) -> str:
        existing = next((i for i in self.store.watchlist(user.chat_id) if i.rodzaj == kind and i.wartosc == value), None)
        if existing:
            self.store.remove_watch(user.chat_id, existing.id)
            toast = f"Przestajesz obserwować: {label}"
        else:
            self.store.add_watch(user.chat_id, kind, value, label)
            toast = f"👀 Obserwujesz: {label} – o nowych inwestycjach dam znać od razu"
        self._refresh_keyboard(user, inv, message_id, view="more")
        return toast

    # === Kliknięcia: ⏰ Przypomnij, 📝 Notatka, ⋯ Więcej ================================================

    def _cb_remind(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """``pr:<nr>`` – wybór terminu; ``pr:<nr>:<dni>`` – przypomnienie rano w tym dniu (0 = bez przypomnienia)."""
        number, _, days = arg.partition(":")
        inv = self._lead(number)
        if inv is None:
            return "Nie znaleziono inwestycji"
        if not days:
            self._refresh_keyboard(user, inv, message_id, view="remind")
            return None
        if not days.isdigit() or (int(days) and int(days) not in ui.REMINDER_DAYS):
            return None
        if int(days) == 0:
            self.store.clear_reminder(user.chat_id, inv.id_sprawy)
            toast = "✖️ Bez przypomnienia"
        else:  # rano (godzina raportu porannego) w wybranym dniu – nigdy w nocy
            day = local(self._now()).date() + timedelta(days=int(days))
            due = datetime.combine(day, clock_time(*map(int, self.settings.morning_time.split(":"))), tzinfo=WARSAW)
            self.store.set_reminder(user.chat_id, inv.id_sprawy, due.astimezone(timezone.utc), int(days))
            toast = f"⏰ Przypomnę {due:%d.%m} rano"
        self._refresh_keyboard(user, inv, message_id)
        return toast

    def _cb_note(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """``nt:<nr>`` – dopisz albo pokaż opcje; ``nt:<nr>:e`` – zmień; ``nt:<nr>:d`` – usuń."""
        number, _, action = arg.partition(":")
        inv = self._lead(number)
        if inv is None:
            return "Nie znaleziono inwestycji"
        existing = self.store.note(user.chat_id, inv.id_sprawy)
        if action == "d":
            self.store.delete_note(user.chat_id, inv.id_sprawy)
            self._refresh_keyboard(user, inv, message_id)
            return "🗑️ Notatka usunięta"
        if existing and action != "e":
            self._refresh_keyboard(user, inv, message_id, view="note")
            return None
        self.store.set_awaiting(user.chat_id, f"notatka:{inv.nr}")
        self._send(user.chat_id, ui.note_prompt())
        return "✏️ Napisz notatkę w czacie"

    def _cb_more(self, user: BotUser, arg: str, message_id: int) -> str | None:
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono inwestycji"
        self._refresh_keyboard(user, inv, message_id, view="more")
        return None

    def _cb_back(self, user: BotUser, arg: str, message_id: int) -> str | None:
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono inwestycji"
        self._refresh_keyboard(user, inv, message_id)
        return None

    def _cb_feedback(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """👍 / 👎 pod „⋯ Więcej” – ocena trafności dla pilotażu (bez wpływu na to, co bot wysyła)."""
        number, _, value = arg.partition(":")
        inv = self._lead(number)
        if inv is None or value not in ("0", "1"):
            return None
        self.store.record_event(user.chat_id, "przydatne" if value == "1" else "nieprzydatne", inv.id_sprawy)
        return "Dzięki – to pomaga nam ulepszać Żółtą Tablicę"

    # === Kliknięcia: filtry, tryb, listy ===========================================================

    def _cb_filters(self, user: BotUser, arg: str, message_id: int) -> str | None:
        if arg == "clear":
            self.store.set_filters(user.chat_id, UserFilters(baza=user.filtry.baza))  # bazę firmy pamiętamy
            self._edit_filters(user, message_id)
            return "🧹 Filtry wyczyszczone"
        if arg == "go":  # „Pokaż pasujące” – to już inwestycje, nie ustawienia
            if self._level(user) < FULL:
                self._send_gate(user)
                return None
            self.send_report(user, on_demand=True)
            return None
        if arg == "trade":
            self.api.edit_message_text(user.chat_id, message_id, *ui.trade_picker(user.branza))
            return None
        screens = {"place": lambda f: ui.place_picker(f, self.store.place_options(self.powiat_codes)),
                   "type": ui.type_picker, "vol": ui.volume_picker, "inv": ui.investor_picker,
                   "near": ui.nearby_screen}
        if arg in screens:
            text, markup = screens[arg](user.filtry)
            self.api.edit_message_text(user.chat_id, message_id, text, markup)
        else:
            self._edit_filters(user, message_id)
        return None

    def _cb_place(self, user: BotUser, arg: str, message_id: int) -> str | None:
        if arg == "txt":
            return self._ask(user, "miejsce")
        powiaty = tuple(p for p in user.filtry.powiaty if p != arg)
        if powiaty == user.filtry.powiaty:
            powiaty += (arg,)
        # wybór powiatów zastępuje promień „📍 Blisko mnie” (baza zostaje zapamiętana)
        return self._update_filters(user, replace(user.filtry, powiaty=powiaty, promien_km=None), message_id, "place")

    def _cb_trade(self, user: BotUser, arg: str, message_id: int) -> str | None:
        trade = get_trade(arg)
        if trade is None and arg != "none":
            return None
        self.store.set_trade(user.chat_id, trade.key if trade else None)
        self.api.edit_message_text(user.chat_id, message_id, *ui.trade_picker(trade.key if trade else None))
        if trade is None or trade.months is None or self._level(user) < FULL:
            self._send(user.chat_id, ui.trade_saved_text(trade))
        else:  # od razu pokaż budowy, które już są na etapie tej branży
            self.send_stage_reminder(self.store.get_user(user.chat_id), on_demand=True)  # type: ignore[arg-type]
        return f"🧰 Branża: {trade.label}" if trade else "🔕 Przypomnienia wyłączone"

    def _cb_radius(self, user: BotUser, arg: str, message_id: int) -> str | None:
        if arg == "loc" or (arg.isdigit() and int(arg) > 0 and user.filtry.baza is None):
            self._send(user.chat_id, *ui.location_request())
            return "👇 Wyślij pinezkę przyciskiem na dole ekranu"
        if not arg.isdigit():
            return None
        km = int(arg)
        if km == 0:
            filters = replace(user.filtry, promien_km=None)
            toast = "📍 Promień wyłączony"
        else:
            filters = replace(user.filtry, promien_km=km, powiaty=(), miejsca=())
            toast = f"📍 Szukam do {km} km od Twojej bazy"
        self.store.set_filters(user.chat_id, filters)
        self.api.edit_message_text(user.chat_id, message_id, *ui.nearby_screen(filters))
        return toast

    def _cb_place_remove(self, user: BotUser, arg: str, message_id: int) -> str | None:
        places = tuple(p for index, p in enumerate(user.filtry.miejsca) if str(index) != arg)
        return self._update_filters(user, replace(user.filtry, miejsca=places), message_id, "place")

    def _cb_type(self, user: BotUser, arg: str, message_id: int) -> str | None:
        try:
            key = ui.CATEGORY_CHOICES[int(arg)][0]
        except (ValueError, IndexError):
            return None
        kategorie = tuple(k for k in user.filtry.kategorie if k != key)
        if kategorie == user.filtry.kategorie:
            kategorie += (key,)
        return self._update_filters(user, replace(user.filtry, kategorie=kategorie), message_id, "type")

    def _cb_volume(self, user: BotUser, arg: str, message_id: int) -> str | None:
        if arg == "txt":
            return self._ask(user, "kubatura")
        value = float(arg) if arg.isdigit() and int(arg) > 0 else None
        self._update_filters(user, replace(user.filtry, min_kubatura=value), message_id, None)
        return "📦 Zapisano kubaturę"

    def _cb_investor(self, user: BotUser, arg: str, message_id: int) -> str | None:
        if arg == "txt":
            return self._ask(user, "inwestor")
        value = "firma" if arg == "firm" else None
        self._update_filters(user, replace(user.filtry, inwestor=value), message_id, None)
        return "💼 Zapisano"

    def _cb_mode(self, user: BotUser, arg: str, message_id: int) -> str | None:
        if arg not in ui.MODE_LABELS:
            return None
        self.store.set_mode(user.chat_id, arg)
        text, markup = ui.mode_screen(self.store.get_user(user.chat_id), self.settings)
        self.api.edit_message_text(user.chat_id, message_id, text, markup)
        return f"⏰ Zapisano: {ui.MODE_LABELS[arg]}"

    def _cb_saved_page(self, user: BotUser, arg: str, message_id: int) -> str | None:
        page = int(arg) if arg.isdigit() else 0
        text, markup = self._saved_page(user, page)
        self.api.edit_message_text(user.chat_id, message_id, text, markup)
        return None

    def _cb_watch_delete(self, user: BotUser, arg: str, message_id: int) -> str | None:
        if arg.isdigit():
            self.store.remove_watch(user.chat_id, int(arg))
        text, markup = ui.watch_screen(self.store.watchlist(user.chat_id))
        self.api.edit_message_text(user.chat_id, message_id, text, markup)
        return "Usunięto z obserwowanych"

    def _save_note(self, user: BotUser, number: str, text: str) -> None:
        """Prywatna notatka z czatu: do ``NOTE_LIMIT`` znaków (dłuższa – prośba o skrócenie), pusta = usuń."""
        inv = self._lead(number)
        if inv is None:
            return
        note = text.strip()
        if len(note) > ui.NOTE_LIMIT:
            self.store.set_awaiting(user.chat_id, f"notatka:{number}")
            self._send(user.chat_id, ui.note_too_long_text(len(note)))
            return
        if note:
            self.store.set_note(user.chat_id, inv.id_sprawy, note)
        else:
            self.store.delete_note(user.chat_id, inv.id_sprawy)
        self._send_card(user, inv)

    def _text_input(self, user: BotUser, text: str) -> None:
        what = user.oczekuje_na
        self.store.set_awaiting(user.chat_id, None)
        if what and what.startswith("notatka:"):
            self._save_note(user, what.partition(":")[2], text)
            return
        filters = user.filtry
        value = " ".join(text.split())[:60]
        if what == "miejsce":
            # spoza monitorowanego obszaru albo literówka; bez żadnych danych (świeża instalacja) nie ma z czym
            # porównać – wtedy przyjmujemy nazwę, a nietrafioną widać potem w podsumowaniu filtrów
            if self.store.has_investments() and not self.store.place_is_known(value):
                self.store.set_awaiting(user.chat_id, "miejsce")
                self._send(user.chat_id, ui.place_unknown_text(value, self._region_label()))
                return
            if user.konfiguracja == "obszar":  # pierwsze kroki: dokładnie ta miejscowość
                self.store.set_filters(user.chat_id, replace(filters, miejsca=(value,), powiaty=(), promien_km=None))
                self._finish_setup(user)
                return
            filters = replace(filters, miejsca=tuple(dict.fromkeys((*filters.miejsca, value))), promien_km=None)
            note = f"✅ Dodano miejsce: {value}\n\n"
        elif what == "inwestor":
            filters = replace(filters, inwestor=value)
            note = f"✅ Szukam inwestorów: „{value}”\n\n"
        elif what == "kubatura":
            volume = _parse_volume(value)
            if volume is None:
                self.store.set_awaiting(user.chat_id, "kubatura")
                self._send(user.chat_id, "🤔 Podaj samą liczbę, np. <b>5000</b>.")
                return
            filters = replace(filters, min_kubatura=volume)
            note = f"✅ Kubatura od {volume:,.0f} m³\n\n".replace(",", " ")
        else:
            return
        self.store.set_filters(user.chat_id, filters)
        self._show_filters(self.store.get_user(user.chat_id), prefix=escape_html(note))

    # === Doręczanie ==============================================================================

    def deliver_instant(self) -> int:
        """Alerty watchlisty (wszyscy) i leady trybu „⚡ od razu”; zwraca liczbę wysłanych wiadomości.

        W ciszy nocnej czekają do rana. Nieudana wysyłka nie jest zapisywana jako doręczona, więc wraca
        w kolejnym cyklu.
        """
        if self._quiet(self._now()):
            return 0
        sent = 0
        for user in self._subscribers():
            if self.should_stop():
                break
            try:
                sent += self._deliver_instant_to(user)
            except TelegramApiError as exc:
                self._delivery_failed(user, exc)
        return sent

    def deliver_personal_reminders(self) -> int:
        """„⏰ Przypomnij”: rano w wybranym dniu, nigdy w nocy; kilka zaległych naraz – zbiorczo, od razu wszystkie.

        Bez dostępu albo przy pauzie przypomnienia czekają (nie giną). Doręczenie „co najmniej raz”:
        awaria między wysyłką a skasowaniem przypomnienia może je powtórzyć po restarcie.
        """
        now = self._now()
        if self._quiet(now):
            return 0
        sent = 0
        for chat_id, leads in self.store.due_reminders(now).items():
            if self.should_stop():
                break
            user = self.store.get_user(chat_id)
            if user is None or not self._receives_automatic(user):
                continue
            distance = user.filtry.distance_km if user.filtry.baza else None

            def delivered(chunk: Sequence[Investment], chat_id: int = chat_id) -> None:
                for inv in chunk:
                    self.store.clear_reminder(chat_id, inv.id_sprawy)

            try:
                if len(leads) == 1:
                    self._send_card(user, leads[0], header=ui.reminder_header())
                    delivered(leads)
                else:
                    self._send_in_chunks(chat_id, leads, lambda chunk: ui.reminders_digest(chunk, distance), delivered)
            except TelegramApiError as exc:
                self._delivery_failed(user, exc)
                continue
            sent += 1
        return sent

    def _send_in_chunks(self, chat_id: int, items: Sequence[Any], build: Callable[[Sequence[Any]], tuple[str, dict]],
                        delivered: Callable[[Sequence[Any]], None]) -> None:
        """Długa lista w kilku wiadomościach wysłanych od razu: do ``max_leads_in_report`` pozycji i do limitu
        Telegrama każda; ``delivered`` odnotowuje każdą część zaraz po jej wysłaniu."""
        start = 0
        while start < len(items):
            count = min(self.settings.max_leads_in_report, len(items) - start)
            text, markup = build(items[start:start + count])
            while len(text) > TELEGRAM_LIMIT and count > 1:
                count = max(1, count - 3)
                text, markup = build(items[start:start + count])
            self._send(chat_id, text, markup)
            delivered(items[start:start + count])
            start += count

    def _receives_automatic(self, user: BotUser) -> bool:
        """Czy do tej osoby idą automatyczne wiadomości – ta sama reguła co ``BotStore.subscribers``."""
        return user.status == "aktywny" and not user.wstrzymane and self._has_access(user)

    def deliver_reports(self, mode: str) -> int:
        """Raporty zbiorcze teraz dla trybu ``mode`` (``rano``/``wieczor``) – przez kolejkę wysyłek."""
        job = next(name for name, tryb in REPORT_JOBS.items() if tryb == mode)
        self._start_sends(job, self._now(), manual=True)
        return self.process_sends()

    def send_report(self, user: BotUser, *, on_demand: bool = False,
                    leads: Sequence[Investment] | None = None) -> bool:
        """Wysyła raport: podsumowanie + lista pasujących leadów z numerami do kliknięcia.

        Za „widziane” uznawane są tylko leady faktycznie pokazane na liście (reszta trafi do kolejnego
        raportu); niepasujące do filtrów – jako pominięte. Gdy nowych pasujących brak, raport na żądanie
        pokazuje pasujące z ostatnich ``recent_days`` dni, żeby po zmianie filtrów od razu było coś widać.
        """
        candidates = self.store.candidates(user.chat_id, self._window_start(user))
        if leads is None:
            leads = [inv for inv in candidates if self._wanted(user, inv)]
        nearest = user.filtry.distance_km if user.filtry.radius_active else None
        distance = user.filtry.distance_km if user.filtry.baza else None
        leads = _ranked(leads, nearest)
        chosen = {inv.id_sprawy for inv in leads}
        skipped = [inv for inv in candidates if inv.id_sprawy not in chosen]
        now_iso = _utc_iso(self.repo.now())
        watched = self.store.deliveries_since(user.chat_id, self._report_since(user), "watchlista")
        if not leads and not on_demand:
            self.store.record_delivery(user.chat_id, skipped, "pominiety")
            self.store.mark_report(user.chat_id, now_iso)
            return False
        if not leads:  # na żądanie, a nic nowego nie pasuje – przegląd historii (pełny, stronami)
            head = ui.nothing_new_head(total_new=len(candidates), watched=watched, note=self._import_note())
            text, markup = self._history_screen(user, 0, head=head)
            self._send(user.chat_id, text, markup)
            self.store.record_delivery(user.chat_id, skipped, "pominiety")
            self.store.mark_report(user.chat_id, now_iso)
            return True
        freshness = self._freshness()

        def build(count: int) -> tuple[str, dict | None]:
            return ui.report(
                f"{local(self._now()):%d.%m}", total_new=len(candidates), leads=leads[:count], matching=len(leads),
                hot=sum(1 for inv in leads if inv.priorytet == HOT), watched=watched, distance=distance,
                freshness=freshness,
            )

        shown = min(len(leads), self.settings.max_leads_in_report)
        text, markup = build(shown)
        while len(text) > TELEGRAM_LIMIT and shown > 1:  # długie opisy/adresy – mniej pozycji na liście
            shown = max(1, shown - 3)
            text, markup = build(shown)
        self._send(user.chat_id, text, markup)
        self.store.record_delivery(user.chat_id, leads[:shown], "raport")
        self.store.record_delivery(user.chat_id, skipped, "pominiety")
        self.store.mark_report(user.chat_id, now_iso)
        return True

    def _history_matches(self, user: BotUser) -> list[Investment]:
        """Wszystkie inwestycje z ostatnich ``recent_days`` dni pasujące do filtrów (także już wysłane)."""
        date_from = (local(self._now()).date() - timedelta(days=self.settings.recent_days)).isoformat()
        matches = [inv for inv in self.store.recent_leads(user.chat_id, date_from) if self._wanted(user, inv)]
        return _ranked(matches, user.filtry.distance_km if user.filtry.radius_active else None)

    def _history_screen(self, user: BotUser, page: int, *, head: Sequence[str] = ()) -> tuple[str, dict | None]:
        """Strona przeglądu historii; nic nie oznacza jako wysłane (kolejka nowych zostaje nietknięta)."""
        matches = self._history_matches(user)
        pages = max(1, -(-len(matches) // ui.HISTORY_PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        start = page * ui.HISTORY_PAGE_SIZE
        return ui.history_page(
            matches[start:start + ui.HISTORY_PAGE_SIZE], page=page, pages=pages, total=len(matches),
            days=self.settings.recent_days, head=head,
            distance=user.filtry.distance_km if user.filtry.baza else None,
            filters=ui.filters_summary(user, self._place_names()), freshness=self._freshness(),
        )

    def _freshness(self) -> str:
        """„🕒 Rejestr GUNB sprawdzony: …” – ostatni import zakończony w całości."""
        checked = self.store.job_time(LAST_IMPORT_JOB)
        return ui.freshness_line(self._local_date(_utc_iso(checked), with_time=True) if checked else None)

    def _import_note(self) -> str | None:
        """Import w toku albo nieudany – żeby „nic nowego” nie znaczyło „nic się nie wydarzyło”."""
        status = self.store.job_status(IMPORT_JOB)
        state = status.stan if status else None
        if state == "trwa" and self.repo.lease_holder(IMPORT_LEASE) is None:
            state = "pominieto"  # „w toku” bez blokady = import przerwany; ponowienie jest zaplanowane
        retry_at = self.store.job_time(FETCH_RETRY_JOB)
        return ui.import_note(state, retry_at=f"{local(retry_at):%H:%M}" if retry_at else None)

    def _cb_history(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """„◀️ Wstecz / Dalej ▶️” w przeglądzie historii – edytuje tę samą wiadomość."""
        text, markup = self._history_screen(user, int(arg) if arg.isdigit() else 0)
        self.api.edit_message_text(user.chat_id, message_id, text, markup)
        return None

    def deliver_stage_reminders(self) -> int:
        """Przypomnienia „⏰ Kiedy dzwonić” teraz (osoby z wybraną branżą) – przez kolejkę wysyłek."""
        self._start_sends(STAGE_REMINDER_JOB, self._now(), manual=True)
        return self.process_sends()

    def send_stage_reminder(self, user: BotUser, *, on_demand: bool = False) -> bool:
        """Budowy (zgodne z filtrami), które dziś są na etapie branży użytkownika – każda raz na branżę.

        Pokazane pozycje są zapisywane z rewizją ``etap:<branża>``; reszta (ponad limit listy) przyjdzie
        kolejnego ranka, bo okna etapów trwają tygodniami.
        """
        trade = get_trade(user.branza)
        if trade is None or trade.months is None:
            return False
        today = local(self._now()).date()
        rewizja = f"etap:{trade.key}"
        since = (today - timedelta(days=LONGEST_WINDOW_DAYS)).isoformat()
        due = [inv for inv in self.store.stage_candidates(user.chat_id, rewizja, since)
               if is_due(trade, inv, today) and self._wanted(user, inv)]
        if not due:
            if on_demand:
                self._send(user.chat_id, ui.stage_none_text(trade))
            return False
        filters = user.filtry
        due = _ranked(due, filters.distance_km if filters.radius_active else None)
        distance = filters.distance_km if filters.baza else None
        shown = min(len(due), self.settings.max_leads_in_report)
        text, markup = ui.stage_reminder(trade, due[:shown], len(due), distance)
        while len(text) > TELEGRAM_LIMIT and shown > 1:
            shown = max(1, shown - 3)
            text, markup = ui.stage_reminder(trade, due[:shown], len(due), distance)
        self._send(user.chat_id, text, markup)
        self.store.record_delivery(user.chat_id, due[:shown], "etap", rewizja=rewizja)
        return True

    def _deliver_instant_to(self, user: BotUser) -> int:
        candidates = self.store.candidates(user.chat_id, self._window_start(user))
        watch_items = self.store.watchlist(user.chat_id)
        sent, remaining, hits = 0, [], []
        for inv in candidates:
            hit = watch_match(inv, watch_items) if watch_items else None
            if hit is None:
                remaining.append(inv)
            else:
                hits.append((hit, inv))
        if len(hits) > WATCH_DIGEST_AFTER:  # np. po wznowieniu – zbiorczo zamiast serii kart, od razu wszystkie
            distance = user.filtry.distance_km if user.filtry.baza else None
            self._send_in_chunks(
                user.chat_id, hits, lambda chunk: ui.watch_digest(chunk, distance),
                lambda chunk: self.store.record_delivery(user.chat_id, [inv for _, inv in chunk], "watchlista"),
            )
            sent += 1
            hits = []
        for hit, inv in hits:
            self._send_card(user, inv, header=ui.watch_header(hit, inv))
            self.store.record_delivery(user.chat_id, [inv], "watchlista")
            sent += 1
        if user.tryb != "natychmiast":
            return sent
        wanted = [inv for inv in remaining if self._wanted(user, inv)]
        if len(wanted) > self.digest_threshold:
            return sent + int(self.send_report(user, leads=wanted))
        for inv in wanted:
            self._send_card(user, inv)
            self.store.record_delivery(user.chat_id, [inv], "natychmiast")
            sent += 1
        return sent

    def _delivery_failed(self, user: BotUser, exc: TelegramApiError) -> None:
        if exc.blocked:
            log.info("Użytkownik %s zablokował bota – wstrzymuję wysyłkę", user.chat_id)
            self.store.set_status(user.chat_id, "zablokowany")
        else:
            log.warning("Nie udało się wysłać do %s: %s", user.chat_id, exc)

    # === Pomocnicze ===============================================================================

    def _wanted(self, user: BotUser, inv: Investment) -> bool:
        return user.filtry.matches(inv) and (not user.tylko_hot or inv.priorytet == HOT)

    def _window_start(self, user: BotUser) -> str:
        oldest = _utc_iso(self.repo.now() - timedelta(days=self.max_age_days))
        return max(user.nowe_od, oldest)

    def _report_since(self, user: BotUser) -> str:
        return max(self._window_start(user), user.ostatni_raport or "")

    def _lead(self, arg: str) -> Investment | None:
        return self.repo.get_by_nr(int(arg)) if arg.isdigit() else None

    def _card(self, user: BotUser, inv: Investment, header: tuple[str, str, str] | None = None) -> tuple[str, dict]:
        """Karta inwestycji; odległość i prywatna notatka mają zarezerwowane miejsce (HTML nigdy nie jest cięty)."""
        extras = ""
        km = user.filtry.distance_km(inv)
        if km is not None:
            extras += f"\n📏 {escape_html(ui.distance_label(km))} w linii prostej od Twojej bazy"
        note = self.store.note(user.chat_id, inv.id_sprawy)
        if note:  # prywatna – tylko w karcie tej osoby
            extras += ui.note_line(note)
        message = self.formatter.telegram(inv, self.repo.last_status_change(inv.id_sprawy), header=header,
                                          limit=TELEGRAM_LIMIT - len(extras))
        return message.text + extras, self._keyboard(user, inv)

    def _keyboard(self, user: BotUser, inv: Investment, view: str = "main") -> dict:
        items = self.store.watchlist(user.chat_id)
        key = investor_key(inv.inwestor)
        remind_at = self.store.reminder(user.chat_id, inv.id_sprawy)
        return ui.lead_keyboard(
            inv,
            flags=self.store.lead_flags(user.chat_id, inv.id_sprawy),
            watching_investor=any(i.rodzaj == "inwestor" and i.wartosc == key for i in items) if key else False,
            watching_gmina=any(i.rodzaj == "gmina" and i.wartosc == inv.gmina_teryt for i in items),
            reminder_on=f"{local(remind_at):%d.%m}" if remind_at else None,
            has_note=self.store.note(user.chat_id, inv.id_sprawy) is not None,
            view=view,
        )

    def _send_card(self, user: BotUser, inv: Investment, header: tuple[str, str, str] | None = None) -> None:
        text, markup = self._card(user, inv, header)
        self._send(user.chat_id, text, markup)

    def _refresh_keyboard(self, user: BotUser, inv: Investment, message_id: int, view: str = "main") -> None:
        self.api.edit_message_reply_markup(user.chat_id, message_id, self._keyboard(user, inv, view))

    def _edit_filters(self, user: BotUser, message_id: int) -> None:
        text, markup = ui.filters_screen(self.store.get_user(user.chat_id), self._place_names(), self.settings)
        self.api.edit_message_text(user.chat_id, message_id, text, markup)

    def _update_filters(self, user: BotUser, filters: UserFilters, message_id: int, screen: str | None) -> None:
        self.store.set_filters(user.chat_id, filters)
        if screen is None:
            self._edit_filters(user, message_id)
            return None
        return self._cb_filters(self.store.get_user(user.chat_id), screen, message_id)

    def _ask(self, user: BotUser, what: str) -> str:
        self.store.set_awaiting(user.chat_id, what)
        self._send(user.chat_id, ui.prompt_text(what))
        return "✏️ Napisz odpowiedź w czacie"

    def _saved_page(self, user: BotUser, page: int) -> tuple[str, dict | None]:
        leads = self.store.saved(user.chat_id, limit=ui.SAVED_PAGE_SIZE, offset=page * ui.SAVED_PAGE_SIZE)
        distance = user.filtry.distance_km if user.filtry.baza else None
        return ui.saved_list(leads, page, self.store.saved_count(user.chat_id), distance)

    def _place_names(self) -> dict[str, str]:
        return dict(self.store.place_options(self.powiat_codes))

    def _send(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        self.api.send_message(chat_id, text, markup)

    def _send_safely(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        try:
            self._send(chat_id, text, markup)
        except TelegramApiError as exc:
            log.warning("Nie udało się wysłać do %s: %s", chat_id, exc)

    def _fetch(self) -> None:
        """Pobiera dane GUNB (stan widoczny dla admina w ``/status``); po niepowodzeniu planuje ponowienie.

        Ponowienie jest zapisane już przed startem – gdy proces zginie w trakcie (kill -9, brak prądu),
        import wróci najpóźniej po godzinie. Import innego procesu (np. historii z instalatora) zostawiamy
        w spokoju, łącznie z jego stanem.
        """
        if self.fetcher is None:
            return
        holder = self.repo.lease_holder(IMPORT_LEASE)
        if holder is not None:
            log.info("Pobieranie danych GUNB pominięte: trwa inny import (%s) – ponowię za godzinę", holder)
            self.store.set_job_time(FETCH_RETRY_JOB, self._now() + FETCH_RETRY_AFTER)
            return
        self.store.set_job_time(FETCH_RETRY_JOB, self._now() + FETCH_RETRY_AFTER)
        self.store.job_started(IMPORT_JOB)
        retry_in: timedelta | None = FETCH_RETRY_AFTER
        try:
            result = self.fetcher()
        except ImportSkipped as exc:
            log.info("Pobieranie danych GUNB pominięte: %s", exc)
            self.store.job_finished(IMPORT_JOB, "pominieto", str(exc))
            retry_in = exc.retry_in
        except (HttpError, GunbFormatError, EmptyImport) as exc:  # znana awaria po stronie GUNB – bez śladu stosu
            log.error("Pobieranie danych GUNB nie powiodło się (ponowię za godzinę): %s", exc)
            self.store.job_finished(IMPORT_JOB, "blad", str(exc))
        except Exception as exc:  # np. zablokowana baza – bot działa dalej, pobieranie wróci za godzinę
            log.exception("Pobieranie danych GUNB przerwane nieoczekiwanym błędem – ponowię za godzinę")
            self.store.job_finished(IMPORT_JOB, "blad", f"{type(exc).__name__}: {exc}")
        else:
            if result is False:
                self.store.job_finished(IMPORT_JOB, "blad", "nieudane pobieranie")
            else:
                self.store.job_finished(IMPORT_JOB, "ok", result if isinstance(result, str) else None)
                self.store.set_job_time(LAST_IMPORT_JOB, self._now())
                retry_in = None
        self.store.set_job_time(FETCH_RETRY_JOB, self._now() + retry_in if retry_in is not None else None)

    def recover_interrupted_import(self) -> None:
        """Po starcie: import „w toku” bez ważnej blokady to import przerwany (np. zabity proces) –
        oznaczamy go i ponawiamy od razu, zamiast czekać na jutrzejszy termin."""
        status = self.store.job_status(IMPORT_JOB)
        if status is None or status.stan != "trwa" or self.repo.lease_holder(IMPORT_LEASE) is not None:
            return
        log.warning("Poprzedni import danych GUNB został przerwany – ponawiam")
        self.store.job_finished(IMPORT_JOB, "pominieto", "przerwany (restart programu)")
        if self.fetcher is not None:
            self.store.set_job_time(FETCH_RETRY_JOB, self._now())

    def _due(self, name: str, now: datetime, hhmm: str) -> bool:
        """Czy zadanie o ``hhmm`` (czas polski) jest dziś już po terminie i jeszcze go nie zrobiono."""
        scheduled = at_local_time(now, hhmm)
        if now < scheduled:
            return False
        last = self.store.job_time(name)
        return last is None or last < scheduled


class JobsWorker(threading.Thread):
    """Wątek zadań w tle: import GUNB, raporty, przypomnienia, kolejka wysyłek, alerty „od razu”.

    Bot zadań powstaje w tym wątku (``make_bot`` zwraca go razem z funkcją sprzątającą) – z własnym
    połączeniem SQLite i własnymi klientami HTTP, bo połączeń SQLite nie dzieli się między wątkami.
    Zadania są sprawdzane co ``every`` sekund; ``stop`` kończy pętlę po bieżącym kroku (wysyłki – po
    bieżącej osobie, import – po bieżącej stronie).
    """

    def __init__(self, make_bot: Callable[[], tuple[LeadBot, Callable[[], None]]], stop: threading.Event,
                 *, every: float = 20.0) -> None:
        super().__init__(name="zadania-bota", daemon=True)
        self._make_bot = make_bot
        self._stop_event = stop
        self._every = every

    def run(self) -> None:
        bot, close = self._make_bot()
        bot.should_stop = self._stop_event.is_set
        try:
            bot.recover_interrupted_import()
            while not self._stop_event.is_set():
                try:
                    bot.run_due_jobs()
                except Exception:  # błąd jednego cyklu nie zatrzymuje wątku
                    log.exception("Błąd zadania harmonogramu bota")
                self._stop_event.wait(self._every)
        finally:
            close()
            log.info("Wątek zadań zakończony")


_CALLBACKS: dict[str, Callable[..., str | None]] = {
    "o": LeadBot._cb_open,
    "s": LeadBot._cb_save,  # starsze przyciski (sprzed rozdzielenia oznaczeń) – zawsze „zapisz”
    "s1": LeadBot._cb_save,
    "s0": LeadBot._cb_unsave,
    "r": LeadBot._cb_reviewed,
    "r1": LeadBot._cb_reviewed,
    "r0": LeadBot._cb_unreviewed,
    "h": LeadBot._cb_hide,
    "u": LeadBot._cb_unhide,
    "hp": LeadBot._cb_history,
    "wi": LeadBot._cb_watch_investor,
    "wg": LeadBot._cb_watch_gmina,
    "f": LeadBot._cb_filters,
    "fp": LeadBot._cb_place,
    "fpr": LeadBot._cb_place_remove,
    "fr": LeadBot._cb_radius,
    "fb": LeadBot._cb_trade,
    "ft": LeadBot._cb_type,
    "fv": LeadBot._cb_volume,
    "fi": LeadBot._cb_investor,
    "m": LeadBot._cb_mode,
    "sv": LeadBot._cb_saved_page,
    "wd": LeadBot._cb_watch_delete,
    "ts": LeadBot._cb_trial_start,
    "ob": LeadBot._cb_setup_trade,
    "oa": LeadBot._cb_setup_area,
    "st": LeadBot._cb_settings,
    "pr": LeadBot._cb_remind,
    "nt": LeadBot._cb_note,
    "mx": LeadBot._cb_more,
    "bk": LeadBot._cb_back,
    "fu": LeadBot._cb_feedback,
}
_CALLBACK_LEVELS: dict[str, int] = {
    **{prefix: SETUP for prefix in ("f", "fp", "fpr", "fr", "fb", "ft", "fv", "fi", "m", "ob", "oa")},
    "ts": NONE,
    "st": NONE,  # ekran ustawień sam sprawdza poziom dla każdego przycisku
}
"""Poziom uprawnień przycisków (domyślnie pełny dostęp – inwestycje, zapisane, obserwowane)."""


def _ranked(leads: Sequence[Investment], nearest: Callable[[Investment], float | None] | None = None) -> list[Investment]:
    """Najpierw 🔥 HOT i więcej punktów, w obrębie tego samego – najnowsze.

    Z „📍 Blisko mnie” (``nearest`` = odległość od bazy): najpierw 🔥 HOT, a w każdej grupie od najbliższych.
    """
    newest_first = sorted(leads, key=lambda i: i.data_aktualizacji or "", reverse=True)
    if nearest is not None:
        def by_distance(inv: Investment) -> tuple[int, float]:
            km = nearest(inv)
            return _PRIORITY_RANK.get(inv.priorytet or "", 3), km if km is not None else float("inf")
        return sorted(newest_first, key=by_distance)
    return sorted(newest_first, key=lambda i: (_PRIORITY_RANK.get(i.priorytet or "", 3), -(i.punkty or 0)))


def _is_chat_id(text: str) -> bool:
    """ID czatu Telegram: liczba całkowita (grupy mają minus)."""
    return text.lstrip("-").isdigit() and len(text) <= 20


def _parse_volume(text: str) -> float | None:
    """„10000”, „10 000”, „10 tys.”, „10k” → 10000.0; ``None`` gdy to nie liczba."""
    cleaned = text.lower().replace(" ", "").replace("\xa0", "").replace(",", ".").removesuffix("m3").removesuffix("m³")
    multiplier = 1
    for suffix in ("tys.", "tys", "k"):
        if cleaned.endswith(suffix):
            cleaned, multiplier = cleaned[: -len(suffix)], 1000
            break
    try:
        value = float(cleaned) * multiplier
    except ValueError:
        return None
    return value if value > 0 else None


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
