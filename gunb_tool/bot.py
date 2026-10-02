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
import re
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
from .ranking import match_reasons, ranked, stage_note
from .bot_ui import BOT_COMMANDS, MENU_BUTTONS
from .clock import WARSAW, at_local_time, local
from .config import BotConfig
from .demo import demo_leads
from .funnel import ACTIVATION, activation_time, trial_cohort
from .exporter import TELEGRAM_LIMIT, MessageFormatter, escape_html
from .gunb_scraper import GunbFormatError
from .http_client import HttpError
from .models import Investment
from .pipeline import IMPORT_LEASE, EmptyImport, ImportSkipped
from .scoring import HOT
from .stages import DAYS_PER_MONTH, LONGEST_WINDOW_DAYS, get_trade, is_due
from .storage import LeadRepository
from .telegram_api import TelegramApiError

log = logging.getLogger(__name__)

__all__ = ["LeadBot", "JobsWorker", "MENU_BUTTONS", "BOT_COMMANDS"]

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
RECEIVE_RETRY_MAX = 60
"""Najdłuższa przerwa (s) między próbami odbioru, gdy Telegram długo nie odpowiada (5, 10, 20, 40, 60…)."""
REPORT_JOBS: dict[str, str] = {"raport_rano": "rano", "raport_wieczor": "wieczor"}
"""Zadanie raportu → tryb użytkowników, którzy go dostają."""
PLACE_CONFIRM = "miejsce?:"
"""``oczekuje_na`` z nazwą spoza danych, która czeka na „✅ Tak, zapisz” (albo na inną wpisaną nazwę)."""
CLEANUP_JOB, CLEANUP_TIME = "porzadki", "03:30"
EVENTS_KEPT = timedelta(days=395)
"""Retencja zdarzeń pilotażu (13 miesięcy) – starsze usuwa nocne sprzątanie; zamówienia zostają z kontem."""
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
TRIAL_NUDGE_JOB = "podpowiedz_test"
TRIAL_NUDGE_AFTER = timedelta(hours=48)
"""Po tylu godzinach testu bez aktywacji – jedna spokojna podpowiedź (nie częściej, nie w nocy, nie w pauzie)."""
TRIAL_EXTENSION_MAX_DAYS = 14
NO_ACCESS_TOAST = "⛔ To wymaga aktywnego testu albo dostępu – szczegóły: /konto"
SETUP_DONE_TOAST = "✅ To już ustawione – zmienisz w ⚙️ Ustawienia"
DB_BUSY_RETRIES = 3
"""Ile razy obsłużyć ponownie aktualizację, przy której baza była chwilowo zajęta."""
CONFLICT_JOB = "konflikt_telegrama"
CONFLICT_PAUSE = timedelta(minutes=10)
"""Tyle po ostatnim konflikcie 409 wątek zadań nic nie wysyła (drugi bot z tym samym tokenem)."""
WATCH_DIGEST_AFTER = 3
"""Więcej alertów obserwowanych naraz (np. po wznowieniu powiadomień) idzie jedną wiadomością."""
FIRST_VALUE_SIZE = 5
"""Ile najlepiej dopasowanych inwestycji pokazać zaraz po starcie (reszta – w pełnym przeglądzie)."""
STALE_AFTER = timedelta(hours=48)
"""Rejestr niesprawdzony dłużej niż tyle = dane mogą być nieaktualne (mówimy to wprost)."""
INQUIRY_EVERY = timedelta(hours=24)
"""Pytanie o ofertę trafia do admina najwyżej raz na tyle (kolejne kliknięcia – tylko potwierdzenie)."""
_SOURCE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_ORDER_RE = re.compile(r"(?i)z?-?(\d{1,9})")

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
    "_show_news": FULL, "_show_saved": NONE, "_show_watchlist": FULL,
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
    "/przedluztest": "_cmd_extend_trial",  # /przedluztest <chat_id> <dni 1–14> <powód> – raz na osobę
    "/status": "_cmd_status",  # import GUNB, wątek zadań, wysyłki z ostatniej doby
    "/raport": "_cmd_pilot_report",  # /raport [7|30] – pilotaż: lejek, kohorty testu, płatności
    "/dane": "_cmd_data",  # diagnostyka danych: nazwy i kody obszarów, lokalizacja, braki, historia, oferta
    "/zamowienia": "_cmd_orders",  # otwarte i ostatnio opłacone zamówienia
    "/zaplacone": "_cmd_paid",  # /zaplacone <Z-nr> [uwagi] – płatność otrzymana (dokładnie raz)
    "/wplata": "_cmd_payment",  # /wplata <chat_id> [uwagi] – płatność bez „🛒 Zamawiam” (np. po rozmowie)
    "/anuluj": "_cmd_cancel_order",  # /anuluj <Z-nr>
    "/napisz": "_cmd_write",  # /napisz <chat_id> <tekst> – wiadomość do osoby przez bota (z podziałem na linie)
    "/firma": "_cmd_company",  # /firma <chat_id> <nazwa|-> – firma osoby (raport liczy firmy)
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
        maintenance: nocna konserwacja (kopia bazy), raz dziennie od ``CLEANUP_TIME``; jej błąd trafia
            do logu i nie zatrzymuje wysyłek.
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
        maintenance: Callable[[], object] | None = None,
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
        self.maintenance = maintenance
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

        Kończy się po bieżącym long pollingu, gdy ``should_stop()`` zwróci ``True``. Telegram niedostępny
        przy starcie nie kończy programu (to byłaby pętla restartów) – menu komend ustawi się później.
        """
        menu_ready = self._try_setup()
        log.info("Bot uruchomiony – czekam na wiadomości (Ctrl+C kończy)")
        failures = 0
        while not should_stop():
            try:
                self.poll_once(self.settings.poll_timeout)
                self.store.set_job_time(RECEIVE_HEARTBEAT_JOB, self._now())
                failures = 0
                menu_ready = menu_ready or self._try_setup()
            except TelegramApiError as exc:
                if exc.code == 409:  # drugi proces odbiera aktualizacje tym samym tokenem
                    self._note_conflict(exc)
                    sleep(30)
                else:  # przy długiej awarii rzadziej – mniej prób i mniej wpisów w logu
                    failures += 1
                    delay = min(RECEIVE_RETRY_MAX, 5 * 2 ** min(failures - 1, 4))
                    log.warning("Telegram: %s – ponowię za %d s", exc, delay)
                    sleep(delay)
            except sqlite3.Error as exc:  # np. baza chwilowo zajęta przez VACUUM w wątku zadań
                log.warning("Baza chwilowo niedostępna: %s – ponawiam", exc)
                sleep(1)
            except Exception:  # pętla odbierania nie może paść – bez niej bot jest głuchy
                log.exception("Nieoczekiwany błąd pętli odbierania wiadomości")
                sleep(5)

    def _try_setup(self) -> bool:
        try:
            self.setup()
        except TelegramApiError as exc:
            log.warning("Menu komend nieustawione (%s) – ponowię, gdy Telegram odpowie", exc)
            return False
        return True

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
        if self._due(CLEANUP_JOB, now, CLEANUP_TIME):  # raz dziennie: kopia bazy, kolejka wysyłek nie rośnie
            self.store.set_job_time(CLEANUP_JOB, now)
            self.store.prune_sends(now - SENDS_KEPT)
            self.store.prune_events(now - EVENTS_KEPT)
            self._run_maintenance()
        self.process_sends()
        self.deliver_personal_reminders()
        last = self.store.job_time("natychmiast")
        interval = timedelta(minutes=self.settings.instant_every_minutes)
        if ran or last is None or last + interval <= now:
            self.store.set_job_time("natychmiast", now)
            if self.settings.access != "open":
                self.notify_access_changes()  # co kilka minut – przypomnienie i koniec dostępu bez opóźnień
                self.notify_trial_nudges()
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

    def _run_maintenance(self) -> None:
        if self.maintenance is None:
            return
        try:
            self.maintenance()
        except Exception:  # np. pełny dysk – admin dostaje alert z logu, a raporty idą dalej
            log.exception("Nocna kopia bazy nie powiodła się")

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
            elif job == TRIAL_NUDGE_JOB:
                delivered = self._send_trial_nudge(user)
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
        if job == TRIAL_NUDGE_JOB and not user.tips_enabled:
            return "podpowiedzi wyłączone"
        if job == TRIAL_NUDGE_JOB and self._activated(user):
            return "test już przynosi efekty"
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
            # /napisz: treść zostaje tak, jak ją napisano (linie, odstępy) – np. dane do przelewu
            args = text.split(maxsplit=2)[1:] if command == "/napisz" else text.split()[1:]
            getattr(self, ADMIN_COMMANDS[command])(chat_id, args)
            return
        user = self.store.get_user(chat_id)
        if command == "/start" or user is None:
            payload = text.split(maxsplit=1)[1] if command == "/start" and len(text.split()) > 1 else None
            self._start(chat_id, message.get("from") or {}, payload)
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
            try:
                self.api.answer_callback_query(callback.get("id"), answer)
            except TelegramApiError as exc:  # np. „query is too old” po wolnej sieci – akcja już się odbyła
                log.warning("Nie potwierdzono kliknięcia %r: %s", data, exc)

    # === Rejestracja i admin ===================================================================

    def _start(self, chat_id: int, sender: dict[str, Any], payload: str | None = None) -> None:
        """Rejestracja: nowa osoba zapisuje się bez dostępu i dostaje opis produktu; admin dostaje jej kartę.

        ``payload`` – parametr z linku ``t.me/<bot>?start=<źródło>``: sprawdzony kod kampanii zapisywany tylko
        przy pierwszym wejściu. Ponowny ``/start`` niczego nie odnawia – ani testu, ani dostępu.
        """
        existed = self.store.get_user(chat_id) is not None
        source = campaign_source(payload)
        user = self.store.register(chat_id, sender.get("first_name"), sender.get("username"), status="aktywny",
                                   backlog_days=self.settings.welcome_backlog_days, zrodlo=source)
        if not existed:
            self.store.record_event(chat_id, "start", szczegoly=source)
        if user.status in ("zablokowany", "oczekuje"):
            self.store.set_status(chat_id, "aktywny")
            user = self.store.get_user(chat_id)
        if user.status == "odrzucony":
            self._send(chat_id, ui.rejected_text(self._contact_html()))
            return
        level = self._level(user)
        if level == NONE:
            self._send(chat_id, *self._gate_screen(user, first=True))
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
        if decision in ("pay", "cancel"):  # przyciski z karty zamówienia – cel to numer zamówienia
            order_id = int(target) if target.isdigit() else 0
            reply = self._confirm_payment(order_id, sender) if decision == "pay" else self._cancel_order(order_id)
            if reply.startswith("✅") or reply.startswith("✖️"):
                self.api.edit_message_text(chat_id, message_id, reply)
            return reply.split("\n", 1)[0][:200]
        user = self.store.get_user(int(target)) if target.lstrip("-").isdigit() else None
        if user is None:
            return "Nie ma takiej osoby"
        if decision == "trial":
            self.api.edit_message_text(chat_id, message_id, self._allow_trial(user))
            return "🎁 Test dostępny"
        if decision == "ok":
            self.api.edit_message_text(chat_id, message_id, self._grant_access(user, days=ui.DEFAULT_PAID_DAYS))
            return "✅ Dostęp nadany ręcznie"
        self.store.set_status(user.chat_id, "odrzucony")
        self.store.revoke_access(user.chat_id)
        self._send_safely(user.chat_id, ui.rejected_text(self._contact_html()))
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
            self._send(admin_chat, self._grant_access(user, days=days, until=until))

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

    def _access_end_after(self, user: BotUser, days: int) -> datetime:
        """Koniec dostępu po dodaniu ``days`` – od końca trwającego dostępu (klient nie traci dni) albo od teraz."""
        now = self._now()
        current = datetime.fromisoformat(user.subscription_ends) \
            if user.subscription_ends and user.has_subscription(_utc_iso(now)) else now
        return current + timedelta(days=days)

    def _grant_access(self, user: BotUser, *, days: int | None = None, until: datetime | None = None) -> str:
        """Dostęp nadany ręcznie przez admina (pilotaż, promocja, wyjątek) – do daty albo na N dni od końca
        trwającego dostępu. To nie płatność: rodzaj ``reczny``, zdarzenie ``dostep_przedluzony``.

        Zastępuje dostęp bez limitu (przełączenie na nowy model); zwraca potwierdzenie dla admina.
        """
        if until is None:
            until = self._access_end_after(user, days or 0)
        ends_iso = _utc_iso(until)
        self.store.set_access(user.chat_id, ends_iso, kind="reczny")
        self.store.record_event(user.chat_id, "dostep_przedluzony", szczegoly="reczny")
        if user.status != "aktywny":  # np. wcześniej odrzucony albo „zablokowany” – admin daje nową szansę
            self.store.set_status(user.chat_id, "aktywny")
        ends_on = self._local_date(ends_iso, with_time=True)
        delivered = self._notify(user.chat_id, ui.activated_text(ends_on, user.imie, days=days), ui.menu_keyboard())
        if delivered and not user.setup_done:
            self._safely(lambda: self._show_setup_step(user))
        return ui.admin_granted_text(user, ends_on, days=days, delivered=delivered)

    def _confirm_payment(self, order_id: int, admin: int | None, note: str | None = None) -> str:
        """Admin potwierdza rzeczywiście otrzymaną płatność: dokładnie raz przedłuża dostęp o okres z zamówienia
        (od końca trwającego dostępu) i potwierdza to klientowi. Zwraca odpowiedź dla admina."""
        order = self.store.get_order(order_id)
        if order is None:
            return "🤔 Nie ma takiego zamówienia"
        with self.repo.transaction():
            renewal = self.store.last_paid_order(order.chat_id) is not None
            confirmed = self.store.confirm_order(order_id, admin=admin or 0, note=note)
            if confirmed is not None:
                user = self.store.get_user(confirmed.chat_id)
                ends_iso = _utc_iso(self._access_end_after(user, confirmed.dni))
                self.store.set_access(confirmed.chat_id, ends_iso, kind="platny")
                self.store.set_order_access(confirmed.id, ends_iso)
                self.store.record_event(confirmed.chat_id, "platnosc", szczegoly=confirmed.number)
                if renewal:
                    self.store.record_event(confirmed.chat_id, "odnowienie", szczegoly=confirmed.number)
                if user.status != "aktywny":
                    self.store.set_status(confirmed.chat_id, "aktywny")
        if confirmed is None:
            state = {"oplacone": "jest już opłacone", "anulowane": "jest anulowane – nie przedłużam dostępu"}
            return f"ℹ️ Zamówienie {order.number} {state.get(order.stan, order.stan)}"
        ends_on = self._local_date(ends_iso, with_time=True)
        user = self.store.get_user(confirmed.chat_id)
        delivered = self._notify(user.chat_id, ui.payment_confirmed_text(confirmed, ends_on), ui.menu_keyboard())
        if delivered and not user.setup_done:
            self._safely(lambda: self._show_setup_step(user))
        return ui.admin_payment_text(confirmed, user, ends_on, delivered=delivered)

    def _cancel_order(self, order_id: int) -> str:
        order = self.store.get_order(order_id)
        if order is None:
            return "🤔 Nie ma takiego zamówienia"
        if not self.store.cancel_order(order_id):
            return f"ℹ️ Zamówienie {order.number} jest już {'opłacone' if order.stan == 'oplacone' else 'anulowane'}"
        self.store.record_event(order.chat_id, "zamowienie_anulowane", szczegoly=order.number)
        self._notify(order.chat_id, ui.order_cancelled_text(order, self._contact_html()))
        return f"✖️ Zamówienie {order.number} anulowane"

    def _cmd_paid(self, admin_chat: int, args: list[str]) -> None:
        """``/zaplacone <Z-nr> [uwagi]`` – tylko admin: płatność otrzymana (to samo co przycisk na karcie)."""
        match = _ORDER_RE.fullmatch(args[0]) if args else None
        if match is None:
            self._send(admin_chat, ui.admin_usage_text())
            return
        self._send(admin_chat, self._confirm_payment(int(match.group(1)), admin_chat, " ".join(args[1:]) or None))

    def _cmd_cancel_order(self, admin_chat: int, args: list[str]) -> None:
        match = _ORDER_RE.fullmatch(args[0]) if len(args) == 1 else None
        if match is None:
            self._send(admin_chat, ui.admin_usage_text())
            return
        self._send(admin_chat, self._cancel_order(int(match.group(1))))

    def _cmd_payment(self, admin_chat: int, args: list[str]) -> None:
        """``/wplata <chat_id> [uwagi]`` – tylko admin: płatność otrzymana bez „🛒 Zamawiam” (np. po rozmowie).

        Potwierdza otwarte zamówienie tej osoby, a bez niego zakłada zamówienie z bieżącej oferty (migawka ceny
        i okresu) i od razu je potwierdza. Każde wywołanie to jedna wpłata. Dzięki temu płatność nie trafia do
        dostępu ręcznego (``/aktywuj``) i ``/raport`` liczy ją jako płatność.
        """
        if not args or not _is_chat_id(args[0]):
            self._send(admin_chat, ui.admin_usage_text())
            return
        user = self._known_user(admin_chat, int(args[0]))
        if user is None:
            return
        order = self.store.open_order(user.chat_id)
        if order is None:
            offer = self.settings.offer
            if not offer.complete:
                self._send(admin_chat, ui.admin_payment_needs_offer_text(user, [*offer.problems, *offer.missing()]))
                return
            order, created = self.store.create_order(user.chat_id, offer)
            if created:
                self.store.record_event(user.chat_id, "zamowienie", szczegoly=f"{order.number} od admina")
        self._send(admin_chat, self._confirm_payment(order.id, admin_chat, " ".join(args[1:]) or None))

    def _cmd_orders(self, admin_chat: int, args: list[str]) -> None:
        open_orders = self.store.orders(stan="zgloszone", limit=30)
        paid = self.store.orders(stan="oplacone", limit=10)
        names = {o.chat_id: (self.store.get_user(o.chat_id) or BotUser(o.chat_id, None, None, "", "", False)).display_name
                 for o in (*open_orders, *paid)}
        self._send_long(admin_chat, ui.admin_orders_text(open_orders, paid, names))

    def _cmd_write(self, admin_chat: int, args: list[str]) -> None:
        """``/napisz <chat_id> <tekst>`` – tylko admin: odpowiedź na pytanie, dane do przelewu itp. przez bota.

        Tekst idzie tak, jak go napisano (z podziałem na linie). Za długi wraca do admina – ucięte dane do przelewu
        byłyby gorsze niż żadne.
        """
        if len(args) < 2 or not _is_chat_id(args[0]) or not args[1].strip():
            self._send(admin_chat, ui.admin_usage_text())
            return
        user = self._known_user(admin_chat, int(args[0]))
        if user is None:
            return
        text = ui.admin_message_text(args[1].strip())
        if len(text) > TELEGRAM_LIMIT:
            self._send(admin_chat, ui.admin_message_too_long_text(len(text)))
            return
        delivered = self._notify(user.chat_id, text)
        self._send(admin_chat, f"✉️ Wysłano do {escape_html(user.display_name)} ({user.chat_id})" if delivered
                   else "⚠️ Nie udało się wysłać (zablokował bota?)")

    def _cmd_company(self, admin_chat: int, args: list[str]) -> None:
        """``/firma <chat_id> <nazwa>`` (``-`` czyści) – firma osoby; raport liczy wtedy firmy, nie tylko konta."""
        if len(args) < 2 or not _is_chat_id(args[0]):
            self._send(admin_chat, ui.admin_usage_text())
            return
        user = self._known_user(admin_chat, int(args[0]))
        if user is None:
            return
        name = " ".join(args[1:]).strip()[:80]
        self.store.set_company(user.chat_id, None if name == "-" else name)
        self._send(admin_chat, f"🏢 {escape_html(user.display_name)}: firma "
                               f"{'usunięta' if name == '-' else escape_html(name)}")

    # === Wejście bez dostępu: opis, przykład, oferta, prośba o test, zamówienie ===================

    def _offer_area(self) -> str:
        return self.settings.offer.area or self._region_label()

    def _gate_screen(self, user: BotUser, *, first: bool = False) -> tuple[str, dict | None]:
        """Bez dostępu: po końcu dostępu – informacja z ofertą; nowa osoba – opis produktu (przy ``/start``)
        albo krótka bramka z przyciskami. Nikt nie dostaje komunikatu o „zaległej płatności”."""
        state, ends_on = self._access_state(user)
        offer = self.settings.offer
        if state in ("test_koniec", "platny_koniec"):
            text = ui.access_ended_text(ends_on or "—", trial=state == "test_koniec", offer=offer)
            return text, ui.inline([ui.offer_buttons(offer, order=self.store.open_order(user.chat_id))])
        if state == "wylaczony":
            return ui.access_revoked_text(self._contact_html()), None
        requested_on = self._local_date(user.prosba_o_test) if user.prosba_o_test else None
        markup = ui.intro_keyboard(requested=user.prosba_o_test is not None or user.trial_used)
        if first:
            return ui.intro_text(user.imie, self._offer_area(), offer, requested_on=requested_on), markup
        return ui.gate_short_text(requested_on=requested_on), markup

    def _cb_intro(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """Przyciski opisu produktu (dostępne bez dostępu): przykład, oferta, pomoc, prośba o test, początek."""
        if arg == "demo":
            self.store.record_event(user.chat_id, "demo")
            today = local(self._now()).date()
            leads = demo_leads(today)
            card = ui.lead_card(leads[0], why=["Twoja okolica (przykład)"], estimates=[
                line for line in (ui.scale_line(leads[0]), stage_note(get_trade("dach"), leads[0], today),
                                  "📏 3 km w linii prostej od Twojej bazy (przykład)") if line])
            self._send(user.chat_id, *ui.demo_screen(leads, card))
        elif arg == "oferta":
            self.store.record_event(user.chat_id, "oferta")
            order = self.store.open_order(user.chat_id)
            self._send(user.chat_id, ui.offer_text(self.settings.offer, self._offer_area(), order=order),
                       ui.offer_keyboard(self.settings.offer, order=order, back=self._level(user) < FULL))
        elif arg == "pomoc":
            self._send(user.chat_id, ui.about_text(self._offer_area()), ui.intro_keyboard(
                requested=user.prosba_o_test is not None or user.trial_used or self._level(user) > NONE))
        elif arg == "test":
            return self._request_trial(user)
        elif arg == "start":
            self._send(user.chat_id, *self._gate_screen(user, first=True))
        return None

    def _request_trial(self, user: BotUser) -> str | None:
        """„🙋 Chcę przetestować” – jedno zgłoszenie do admina na osobę; testy uruchamia admin ręcznie."""
        if self._has_access(user):
            return "✅ Masz już dostęp – 📊 Inwestycje"
        if user.trial_available:
            self._send(user.chat_id, *ui.trial_offer(setup_done=user.setup_done))
            if not user.setup_done:
                self._show_setup_step(user)
            return None
        if user.trial_used:
            self._send(user.chat_id, ui.offer_text(self.settings.offer, self._offer_area()),
                       ui.offer_keyboard(self.settings.offer, back=True))
            return "Darmowy test był już wykorzystany – zobacz ofertę"
        if not self.store.request_trial(user.chat_id):
            return f"⏳ Prośba o test jest już wysłana ({self._local_date(user.prosba_o_test)}) – odezwę się tutaj"
        self.store.record_event(user.chat_id, "prosba_o_test", szczegoly=user.zrodlo)
        user = self.store.get_user(user.chat_id) or user
        for admin in self.settings.admins:
            self._send_safely(admin, *ui.admin_trial_request_card(user, self._subscription_label(user)))
        self._send(user.chat_id, ui.trial_requested_text(self.settings.offer))
        return "✅ Prośba wysłana"

    def _cb_order(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """``zm:new`` – zamówienie (tylko przy kompletnej ofercie); ``zm:q`` – pytanie o ofertę do admina."""
        offer = self.settings.offer
        if arg == "new" and offer.complete:
            order, created = self.store.create_order(user.chat_id, offer)
            if created:
                self.store.record_event(user.chat_id, "zamowienie", szczegoly=order.number)
                for admin in self.settings.admins:
                    self._send_safely(admin, *ui.admin_order_card(order, user, self._subscription_label(user)))
            self._send(user.chat_id, ui.order_text(order, offer, again=not created))
            return "🛒 Zamówienie przyjęte" if created else f"🧾 {order.number} czeka na płatność"
        if arg not in ("new", "q"):
            return None
        since = _utc_iso(self._now() - INQUIRY_EVERY)
        if self.store.has_event(user.chat_id, "pytanie_oferta", since=since):
            return "✅ Pytanie już przekazane – odpowiem tutaj"
        self.store.record_event(user.chat_id, "pytanie_oferta")
        for admin in self.settings.admins:
            self._send_safely(admin, ui.admin_inquiry_text(user, self._subscription_label(user)))
        self._send(user.chat_id, ui.inquiry_sent_text(offer))
        return None

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
        """„▶️ Zacznij 7-dniowy test” (``ts``) albo „Rozumiem – zacznij mimo to” (``ts:ok``, przy pustym wyniku) –
        świadomy start po ustawieniach; kolejne kliknięcia nic nie zmieniają, sam z siebie test nie rusza."""
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
        self.store.record_event(user.chat_id, "test_start", szczegoly="mimo_pustych" if arg == "ok" else None)
        ends_on = self._local_date(_utc_iso(now + TRIAL_LENGTH), with_time=True)
        self._send(user.chat_id, ui.trial_started_text(ends_on), ui.menu_keyboard())
        self._send(user.chat_id, *self._first_value(self.store.get_user(user.chat_id) or user))
        return "🎁 Test wystartował"

    # === Pierwsze kroki: branża → obszar → gotowe ========================================================

    def _show_setup_step(self, user: BotUser, message_id: int | None = None) -> None:
        """Bieżący krok pierwszej konfiguracji (nowa wiadomość albo podmiana ``message_id``)."""
        step = user.konfiguracja if user.konfiguracja in SETUP_STEPS else "branza"
        if user.konfiguracja != step:
            self.store.set_setup_step(user.chat_id, step)
        if not self.store.has_event(user.chat_id, "konfiguracja_start"):
            self.store.record_event(user.chat_id, "konfiguracja_start")
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
        """Koniec pierwszej konfiguracji: podsumowanie (zakres danych, ile pasuje, świeżość); przed testem – start
        testu albo, przy pustym wyniku, przyczyna i wybór; z dostępem od razu najlepiej dopasowane."""
        self.store.set_setup_step(user.chat_id, SETUP_DONE)
        self.store.record_event(user.chat_id, "konfiguracja")
        user = self.store.get_user(user.chat_id) or user
        level = self._level(user)
        text, markup = self._summary(user, can_start_trial=level == SETUP)
        if message_id is None:
            self._send(user.chat_id, text, markup or ui.menu_keyboard())
        else:
            self.api.edit_message_text(user.chat_id, message_id, text, markup)
        if level == FULL:
            self._send(user.chat_id, *self._first_value(user))

    def _summary(self, user: BotUser, *, can_start_trial: bool) -> tuple[str, dict | None]:
        recent, in_window = len(self._history_matches(user)), len(self._stage_matches(user))
        notes = [note for note in (self._stale_note(), self._history_gap(user)) if note]
        empty = [] if recent or in_window else self._empty_explanation(user)
        return ui.setup_summary(user, ui.place_label(user.filtry, self._place_names()), self.settings,
                                can_start_trial=can_start_trial, area=self._offer_area(),
                                data_range=self.store.data_range(), freshness=self._freshness(), recent=recent,
                                in_window=in_window, notes=notes, empty=empty)

    def _first_value(self, user: BotUser) -> tuple[str, dict]:
        """Pierwsza wartość: 3–5 najlepiej dopasowanych (okno etapu branży, miejsce, data) bez zużywania nowości."""
        today = local(self._now()).date()
        in_window = self._stage_matches(user)
        recent = self._history_matches(user)
        seen = {inv.id_sprawy for inv in in_window}
        combined = ranked(user, in_window + [inv for inv in recent if inv.id_sprawy not in seen], today)
        if not combined:
            reason = self._empty_reason(user)
            self.store.record_event(user.chat_id, "pusto", szczegoly=reason)
            return ui.first_value_empty(self._empty_explanation(user, reason), days=self.settings.recent_days,
                                        freshness=self._freshness())
        self.store.record_event(user.chat_id, "wyniki", szczegoly=str(len(combined)))
        tip = self._take_tip(user, "otworz", done=self.store.has_event(user.chat_id, "szczegoly"))
        return ui.first_value(combined[:FIRST_VALUE_SIZE], recent=len(recent), in_window_ids=seen,
                              trade=get_trade(user.branza), days=self.settings.recent_days,
                              distance=user.filtry.distance_km if user.filtry.baza else None, tip=tip,
                              freshness=self._freshness())

    def _stage_matches(self, user: BotUser) -> list[Investment]:
        """Inwestycje z ustawień osoby, które według szacunku są dziś w oknie etapu jej branży."""
        trade = get_trade(user.branza)
        if trade is None or trade.months is None:
            return []
        today = local(self._now()).date()
        since = (today - timedelta(days=LONGEST_WINDOW_DAYS)).isoformat()
        return [inv for inv in self.store.decided_since(user.chat_id, since)
                if is_due(trade, inv, today) and self._wanted(user, inv)]

    def _empty_reason(self, user: BotUser) -> str:
        """Dlaczego nic nie pasuje: ``brak_danych``, ``poza_obszarem``, ``miejsce_bez_danych``, ``cisza``, ``filtry``."""
        if not self.store.has_investments():
            return "brak_danych"
        filters = user.filtry
        if filters.radius_active:
            nearest = self.store.nearest_investment_km(filters.baza)  # type: ignore[arg-type]
            if nearest is None or nearest > (filters.promien_km or 0):
                return "poza_obszarem"
        elif filters.miejsca and not filters.powiaty and not any(self.store.place_is_known(m) for m in filters.miejsca):
            return "miejsce_bez_danych"
        date_from = (local(self._now()).date() - timedelta(days=self.settings.recent_days)).isoformat()
        return "cisza" if self.store.recent_count(date_from) == 0 else "filtry"

    def _empty_explanation(self, user: BotUser, reason: str | None = None) -> list[str]:
        reason = reason or self._empty_reason(user)
        date_from = (local(self._now()).date() - timedelta(days=self.settings.recent_days)).isoformat()
        lines = ui.empty_reason_lines(reason, area=self._offer_area(), region_count=self.store.recent_count(date_from),
                                      days=self.settings.recent_days,
                                      filters=ui.filters_summary(user, self._place_names()),
                                      places=user.filtry.miejsca)
        stale = self._stale_note()
        return lines + ([stale] if stale and reason != "brak_danych" else [])

    def _stale_note(self) -> str | None:
        """Import w toku, nieudany albo dawno niesprawdzony rejestr – żeby pusto nie znaczyło „nic się nie dzieje”."""
        note = self._import_note()
        if note:
            return note
        checked = self.store.job_time(LAST_IMPORT_JOB)
        if checked is not None and self._now() - checked > STALE_AFTER:
            return (f"⚠️ Rejestr nie był sprawdzony od {self._local_date(_utc_iso(checked), with_time=True)} – "
                    "dane mogą być nieaktualne.")
        return None

    def _history_gap(self, user: BotUser) -> str | None:
        """Branża z oknem etapu, a w bocie brak decyzji na tyle starych, by jakakolwiek budowa była w oknie."""
        trade = get_trade(user.branza)
        if trade is None or trade.months is None or not self.store.has_investments():
            return None
        data_range = self.store.data_range()
        oldest = data_range[0] if data_range else None
        reachable = (local(self._now()).date() - timedelta(days=round(trade.months[0] * DAYS_PER_MONTH))).isoformat()
        return ui.history_gap_note(trade, oldest) if oldest is None or oldest > reachable else None

    def _take_tip(self, user: BotUser, key: str, *, done: bool) -> bool:
        """Podpowiedź ``key`` pokazujemy raz – i wcale, gdy osoba już to zrobiła albo wyłączyła podpowiedzi."""
        if done or not user.tips_enabled or key in user.porady:
            return False
        self.store.mark_tip(user.chat_id, key)
        return True

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
                for part in ui.split_lines(ui.admin_expired_text(entries)):
                    self._send_safely(admin, part)

    def notify_trial_nudges(self) -> None:
        """Jedna podpowiedź na test: 48 h po starcie, gdy osoba wciąż się nie aktywowała (definicja w ``funnel``).

        Nie w nocy, nie w pauzie, nie po wyłączeniu podpowiedzi; przez kolejkę wysyłek (restart nie dubluje).
        """
        now = self._now()
        if self._quiet(now):
            return
        for user in self.store.trial_nudge_candidates(now - TRIAL_NUDGE_AFTER, now):
            with self.repo.transaction():
                self.store.mark_trial_nudged(user.chat_id, user.test_start or "")
                if not self._activated(user):
                    self.store.enqueue_sends(f"{TRIAL_NUDGE_JOB}:{user.test_start}", [user.chat_id])

    def _activated(self, user: BotUser) -> bool:
        if not user.test_start:
            return False
        start = datetime.fromisoformat(user.test_start)
        return activation_time(self.store.events(start, chat_id=user.chat_id), start) is not None

    def _send_trial_nudge(self, user: BotUser) -> bool:
        summary = self.store.work_summary(user.chat_id, user.test_start or "")
        self._send(user.chat_id, *ui.trial_nudge(summary, self._local_date(user.subscription_ends, with_time=True)))
        self.store.record_event(user.chat_id, "podpowiedz_test")
        return True

    def _cmd_extend_trial(self, admin_chat: int, args: list[str]) -> None:
        """``/przedluztest <chat_id> <dni 1–14> <powód>`` – tylko admin: jednorazowe przedłużenie testu z zapisem
        powodu (np. urlop klienta). Osoba sama testu nie odnowi: ani ``/start``, ani prośbą o test."""
        days = int(args[1]) if len(args) >= 2 and args[1].isdigit() else 0
        reason = " ".join(args[2:]).strip()
        if not args or not _is_chat_id(args[0]) or not 1 <= days <= TRIAL_EXTENSION_MAX_DAYS or len(reason) < 3:
            self._send(admin_chat, ui.admin_usage_text())
            return
        user = self._known_user(admin_chat, int(args[0]))
        if user is None:
            return
        if not user.trial_used or not user.on_trial:
            self._send(admin_chat, f"ℹ️ {escape_html(user.display_name)} nie jest w teście "
                                   f"({escape_html(self._subscription_label(user))}) – przedłużam tylko test.")
            return
        ends_iso = _utc_iso(self._access_end_after(user, days))
        if user.test_przedluzono or not self.store.extend_trial(user.chat_id, datetime.fromisoformat(ends_iso), reason):
            self._send(admin_chat, f"ℹ️ Test {escape_html(user.display_name)} już był przedłużany "
                                   f"(powód: {escape_html(user.test_przedluzenie_powod or '—')}) – drugi raz się nie da.")
            return
        self.store.record_event(user.chat_id, "test_przedluzony", szczegoly=f"{days}d")
        ends_on = self._local_date(ends_iso, with_time=True)
        self._notify(user.chat_id, ui.trial_extended_text(ends_on), ui.menu_keyboard())
        self._send(admin_chat, f"🎁 Test {escape_html(user.display_name)} ({user.chat_id}) przedłużony o {days} dni – "
                               f"do {ends_on} (powód: {escape_html(reason)}).")

    def _send_access_notice(self, user: BotUser, *, ended: bool) -> bool:
        """Dzień przed końcem i po końcu dostępu: termin, oferta (zamówienie albo pytanie) i – w teście –
        podsumowanie rzeczywistych działań z bazy. Wiadomość transakcyjna: przychodzi także w pauzie."""
        ends_on = self._local_date(user.subscription_ends, with_time=True)
        offer = self.settings.offer
        markup = ui.inline([ui.offer_buttons(offer, order=self.store.open_order(user.chat_id))])
        if ended:
            self.store.record_event(user.chat_id, "koniec_dostepu", szczegoly="test" if user.on_trial else "dostep")
            self._send(user.chat_id, ui.access_ended_text(ends_on, trial=user.on_trial, offer=offer), markup)
            return True
        summary = self.store.work_summary(user.chat_id, user.test_start) if user.on_trial and user.test_start else None
        self._send(user.chat_id, ui.access_reminder_text(ends_on, trial=user.on_trial, offer=offer, summary=summary),
                   markup)
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
        if user.trial_available and user.setup_done:  # przed startem: ustawienia, zakres danych, ile pasuje
            text, markup = self._summary(user, can_start_trial=True)
            self._send(user.chat_id, ui.trial_waiting()[0] + "\n\n" + text, markup)
        elif user.trial_available:
            self._send(user.chat_id, *ui.trial_waiting())
        else:
            self._send(user.chat_id, *self._gate_screen(user))

    def _notify(self, chat_id: int, text: str, markup: dict | None = None) -> bool:
        """Wiadomość o zmianie dostępu – błąd wysyłki nie przerywa akcji admina; zwraca, czy doszła."""
        try:
            self._send(chat_id, text, markup)
        except TelegramApiError as exc:
            log.warning("Nie udało się powiadomić %s: %s", chat_id, exc)
            return False
        return True

    def _cmd_pilot_report(self, admin_chat: int, args: list[str]) -> None:
        """``/raport [dni]`` – tylko admin: lejek od wejścia do płatności z ostatnich 1–90 dni: osoby obok zdarzeń,
        dostarczenie osobno od interakcji, dostęp ręczny osobno od płatności, kohorty testu z zakończoną obserwacją."""
        days = int(args[0]) if args and args[0].isdigit() and 1 <= int(args[0]) <= 90 else 7
        now = self._now()
        since = now - timedelta(days=days)
        events = self.store.events(since)
        users = self.store.all_users()
        self._send_long(admin_chat, ui.pilot_report(
            days, since=since, sent=self.store.delivery_counts(since), unique=self.store.event_counts(since),
            events=events, users=users, outcomes=self.store.outcome_counts(since),
            cohort=trial_cohort(users, events, since=since, now=now), rule=ACTIVATION.describe(),
        ))

    def _cmd_data(self, admin_chat: int, args: list[str]) -> None:
        """``/dane`` – tylko admin: co jest w bazie i czego brakuje (bez poprawiania danych zgadywaniem)."""
        overview = self.store.data_overview(self.powiat_codes)
        oldest = overview["najstarsza_decyzja"]
        needed = (local(self._now()).date() - timedelta(days=LONGEST_WINDOW_DAYS)).isoformat()
        self._send_long(admin_chat, ui.data_report(
            overview, place_names=self._place_names(), freshness=self._freshness(), import_note=self._import_note(),
            history_needed=needed if not oldest or oldest > needed else None, offer_missing=self.settings.offer.missing(),
            offer_problems=self.settings.offer.problems,
        ))

    def _cmd_status(self, admin_chat: int, args: list[str]) -> None:
        """``/status`` – tylko admin: stan importu GUNB, wątku zadań i wysyłek z ostatniej doby."""
        now = self._now()
        day_ago = now - timedelta(hours=24)
        self._send_long(admin_chat, ui.status_text(
            now=now, import_status=self.store.job_status(IMPORT_JOB), last_import=self.store.job_time(LAST_IMPORT_JOB),
            retry_at=self.store.job_time(FETCH_RETRY_JOB), heartbeat=self.store.job_time(HEARTBEAT_JOB),
            sends=self.store.send_counts(day_ago), failed=self.store.failed_sends(day_ago),
        ))

    def _has_access(self, user: BotUser) -> bool:
        """Pełny dostęp – ta sama reguła co zapytanie ``BotStore.subscribers`` dla wysyłek w tle."""
        return (user.chat_id in self.settings.admins or self.settings.access == "open" or user.bez_limitu
                or user.has_subscription(_utc_iso(self._now())))

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
        if self._level(user) < FULL and not user.ever_had_access:
            self._send_gate(user)
            return
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
        order = self.store.open_order(user.chat_id)
        text = ui.account_text(state=state, ends_on=ends_on, contact_html=self._contact_html(),
                               offer=self.settings.offer, paid=self.store.last_paid_order(user.chat_id),
                               open_order=order)
        if state == "test_dostepny":
            return text, ui.inline([[ui.START_TRIAL_BUTTON if user.setup_done else ui.SETUP_RESUME_BUTTON]])
        if state in ("admin", "open", "bez_limitu"):
            return text, None
        if state == "brak":
            return text, ui.intro_keyboard(requested=user.prosba_o_test is not None)
        return text, ui.inline([ui.offer_buttons(self.settings.offer, order=order),
                                [("📄 Szczegóły oferty", "i:oferta")]])

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
        if arg == "t":  # 💡 podpowiedzi i podsumowania testu – usługowe dodatki ponad raporty
            self.store.set_tips_enabled(user.chat_id, not user.tips_enabled)
            user = self.store.get_user(user.chat_id) or user
            self.api.edit_message_text(user.chat_id, message_id, *self._settings_screen(user))
            return "💡 Podpowiedzi włączone" if user.tips_enabled else "💡 Podpowiedzi wyłączone"
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
        self._send_long(user.chat_id, ui.users_list(everyone, self._subscription_label))

    def _subscription_label(self, user: BotUser) -> str:
        """Dostęp na liście admina, np. „💳 do 29.10.2026”, „🎁 test do …”, „⌛ wygasł …”, „nieaktywny”."""
        state, ends_on = self._access_state(user)
        day = self._local_date(user.subscription_ends)
        dated = "🔑 ręcznie do" if user.rodzaj_dostepu == "reczny" else "💳 do"
        return {
            "admin": "👑 admin", "open": "✅ otwarty", "bez_limitu": "♾️ bez terminu (dotychczasowy)",
            "test": f"🎁 test do {ends_on}", "platny": f"{dated} {day}", "test_dostepny": "🎁 test czeka na start",
            "test_koniec": f"⌛ test skończył się {day}", "platny_koniec": f"⌛ wygasł {day}",
            "wylaczony": "⛔ wyłączony",
        }.get(state, "🙋 prosi o test" if user.prosba_o_test else "nieaktywny")

    # === Kliknięcia: lead ======================================================================

    def _cb_open(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """Numer z listy – karta inwestycji. Bez dostępu tylko własna praca (zapis, notatka, wynik,
        przypomnienie) w trybie archiwum: stare przyciski i numery nie otwierają nowych danych."""
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono inwestycji"
        if self._level(user) < FULL:
            if not (user.ever_had_access and self.store.owns_work(user.chat_id, inv.id_sprawy)):
                return NO_ACCESS_TOAST
            self._send(user.chat_id, *self._archive_card(user, inv))
            return None
        text, markup = self._card(user, inv)
        if self._take_tip(user, "zapisz", done=self.store.saved_count(user.chat_id) > 0):
            text, markup = self._card(user, inv, limit=TELEGRAM_LIMIT - len(ui.TIP_SAVE) - 2)
            text += "\n\n" + ui.TIP_SAVE
        self._send(user.chat_id, text, markup)
        self.store.record_event(user.chat_id, "szczegoly", inv.id_sprawy)
        return None

    def _cb_details(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """``d:<nr>`` – szczegóły z rejestru w tej samej wiadomości; ``d:<nr>:0`` – z powrotem pierwszy poziom."""
        number, _, mode = arg.partition(":")
        inv = self._lead(number)
        if inv is None:
            return "Nie znaleziono inwestycji"
        text, markup = self._card(user, inv, details=mode != "0")
        self.api.edit_message_text(user.chat_id, message_id, text, markup)
        return None

    def _cb_outcome(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """``w:<nr>`` – wybór wyniku pracy; ``w:<nr>:<kod>`` – ustaw (``0`` – wyczyść); „❌ Niepasująca” pyta o powód."""
        number, _, code = arg.partition(":")
        inv = self._lead(number)
        if inv is None:
            return "Nie znaleziono inwestycji"
        if not code:
            self._refresh_keyboard(user, inv, message_id, view="outcome")
            return None
        if code != "0" and code not in ui.OUTCOME_CODES:
            return None
        wynik = None if code == "0" else ui.OUTCOME_CODES[code]
        if self.store.set_outcome(user.chat_id, inv.id_sprawy, wynik=wynik):
            self.store.record_event(user.chat_id, "wynik", inv.id_sprawy, wynik or "wyczyszczony")
        if wynik == "niepasujaca":
            self._refresh_keyboard(user, inv, message_id, view="reason")
            return "❌ Niepasująca – dlaczego? (jedno kliknięcie, możesz pominąć)"
        self._refresh_keyboard(user, inv, message_id)
        return f"📋 Zapisano: {ui.OUTCOME_LABELS[wynik]}" if wynik else "📋 Wynik wyczyszczony"

    def _cb_reason(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """``wp:<nr>:<kod>`` – powód „niepasującej” (zły obszar, rodzaj, moment, brak działania, błędne dane)."""
        number, _, code = arg.partition(":")
        inv = self._lead(number)
        if inv is None or code not in ui.REASON_CODES:
            return None
        reason = ui.REASON_CODES[code]
        if self.store.set_outcome(user.chat_id, inv.id_sprawy, wynik="niepasujaca", powod=reason):
            self.store.record_event(user.chat_id, "wynik_powod", inv.id_sprawy, reason)
        self._refresh_keyboard(user, inv, message_id)
        return "Dzięki – to pomaga dopasować kolejne inwestycje"

    def _cb_flag(self, user: BotUser, arg: str, message_id: int, *, toast: str, view: str = "main",
                 **flag: bool) -> str:
        """Ustawia jedno oznaczenie na wartość zapisaną w przycisku (idempotentnie) i odświeża przyciski
        widoku, w którym ten przycisk jest (``view``)."""
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono inwestycji"
        self.store.set_lead_flags(user.chat_id, inv.id_sprawy, **flag)
        self._refresh_keyboard(user, inv, message_id, view=view)
        return toast

    def _cb_save(self, user: BotUser, arg: str, message_id: int) -> str:
        """``s1:``/``s:`` (starsze przyciski) – zapisz; ponowione kliknięcie niczego nie cofa."""
        inv = self._lead(arg)
        newly_saved = inv is not None and not self.store.lead_flags(user.chat_id, inv.id_sprawy).saved
        if newly_saved:
            self.store.record_event(user.chat_id, "zapis", inv.id_sprawy)
        toast = self._cb_flag(user, arg, message_id, saved=True, toast="⭐ Zapisano – znajdziesz je pod „⭐ Zapisane”")
        work = self.store.work_summary(user.chat_id, "")
        if newly_saved and self._take_tip(user, "notatka", done=bool(work["notatki"] or work["przypomnienia"])):
            self._send_safely(user.chat_id, ui.TIP_NOTE)
        return toast

    def _cb_unsave(self, user: BotUser, arg: str, message_id: int) -> str:
        return self._cb_flag(user, arg, message_id, saved=False, toast="Usunięto z zapisanych")

    def _cb_reviewed(self, user: BotUser, arg: str, message_id: int) -> str:
        """``r1:``/``r:`` – przejrzane; zapisanie zostaje."""
        return self._cb_flag(user, arg, message_id, reviewed=True, view="more", toast="✅ Oznaczono jako przejrzane")

    def _cb_unreviewed(self, user: BotUser, arg: str, message_id: int) -> str:
        return self._cb_flag(user, arg, message_id, reviewed=False, view="more",
                             toast="Zdjęto oznaczenie „przejrzane”")

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
        """👍 / 👎 pod „⋯ Więcej” – jedna ocena na osobę i inwestycję (ponowione kliknięcie niczego nie dolicza)."""
        number, _, value = arg.partition(":")
        inv = self._lead(number)
        if inv is None or value not in ("0", "1"):
            return None
        if self.store.set_outcome(user.chat_id, inv.id_sprawy, ocena=1 if value == "1" else -1):
            self.store.record_event(user.chat_id, "przydatne" if value == "1" else "nieprzydatne", inv.id_sprawy)
            if message_id is not None:
                self._safely(lambda: self._refresh_keyboard(user, inv, message_id, view="more"))
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

    def _cb_place_confirm(self, user: BotUser, arg: str, message_id: int) -> str | None:
        """„✅ Tak, zapisz” pod nazwą, której nie ma jeszcze w danych – czeka na nią ``oczekuje_na``."""
        pending = user.oczekuje_na or ""
        if not pending.startswith(PLACE_CONFIRM):
            return "⌛ Ten przycisk jest już nieaktualny – wpisz nazwę jeszcze raz"
        self.store.set_awaiting(user.chat_id, None)
        self._save_place(user, pending[len(PLACE_CONFIRM):])
        return None

    def _save_place(self, user: BotUser, value: str) -> None:
        filters = user.filtry
        if user.konfiguracja == "obszar":  # pierwsze kroki: dokładnie ta miejscowość
            self.store.set_filters(user.chat_id, replace(filters, miejsca=(value,), powiaty=(), promien_km=None))
            self._finish_setup(user)
            return
        filters = replace(filters, miejsca=tuple(dict.fromkeys((*filters.miejsca, value))), promien_km=None)
        self.store.set_filters(user.chat_id, filters)
        self._show_filters(self.store.get_user(user.chat_id), prefix=escape_html(f"✅ Dodano miejsce: {value}\n\n"))

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
        if self._level(user) < FULL and not user.ever_had_access:
            return NO_ACCESS_TOAST
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
        if what == "miejsce" or (what or "").startswith(PLACE_CONFIRM):
            # literówka, spoza obszaru albo wieś, z której jeszcze nic nie wpłynęło – zapis dopiero po „✅ Tak”
            # (albo wpisz inną nazwę); bez żadnych danych (świeża instalacja) nie ma z czym porównać – przyjmujemy
            value = value.strip(" .,;:!?\"'")
            if not value:
                self.store.set_awaiting(user.chat_id, "miejsce")
                self._send(user.chat_id, "🤔 Wpisz nazwę miejscowości, np. <b>Dywity</b>.")
                return
            if self.store.has_investments() and not self.store.place_is_known(value):
                self.store.set_awaiting(user.chat_id, PLACE_CONFIRM + value)
                self._send(user.chat_id, *ui.place_unknown(value, self._region_label()))
                return
            self._save_place(user, value)
            return
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
        distance = user.filtry.distance_km if user.filtry.baza else None
        leads = ranked(user, leads, local(self._now()).date())
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
        return ranked(user, matches, local(self._now()).date())

    def _history_screen(self, user: BotUser, page: int, *, head: Sequence[str] = ()) -> tuple[str, dict | None]:
        """Strona przeglądu historii; nic nie oznacza jako wysłane (kolejka nowych zostaje nietknięta)."""
        matches = self._history_matches(user)
        pages = max(1, -(-len(matches) // ui.HISTORY_PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        start = page * ui.HISTORY_PAGE_SIZE
        explanation: list[str] = []
        if not matches:
            reason = self._empty_reason(user)
            explanation = self._empty_explanation(user, reason)
            self.store.record_event(user.chat_id, "pusto", szczegoly=reason)
        elif page == 0:
            self.store.record_event(user.chat_id, "wyniki", szczegoly=str(len(matches)))
        return ui.history_page(
            matches[start:start + ui.HISTORY_PAGE_SIZE], page=page, pages=pages, total=len(matches),
            days=self.settings.recent_days, head=head,
            distance=user.filtry.distance_km if user.filtry.baza else None,
            filters=ui.filters_summary(user, self._place_names()), freshness=self._freshness(),
            explanation=explanation,
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
        due = ranked(user, due, today)
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

    def _card(self, user: BotUser, inv: Investment, header: tuple[str, str, str] | None = None, *,
              details: bool = False, limit: int = TELEGRAM_LIMIT) -> tuple[str, dict]:
        """Karta inwestycji: fakty z rejestru, dlaczego ją widać, szacunki i prywatna notatka (tylko tej osoby)."""
        today = local(self._now()).date()
        estimates = []
        km = user.filtry.distance_km(inv)
        if km is not None:
            estimates.append(f"📏 {ui.distance_label(km)} w linii prostej od Twojej bazy")
        estimates += [line for line in (ui.scale_line(inv), stage_note(get_trade(user.branza), inv, today)) if line]
        change = self.repo.last_status_change(inv.id_sprawy)
        status_change = (change.stary_status.replace("_", " "), change.nowy_status.replace("_", " ")) \
            if change is not None and change.stary_status else None
        text = ui.lead_card(inv, why=match_reasons(user, inv, self._place_names(), today), estimates=estimates,
                            note=self.store.note(user.chat_id, inv.id_sprawy), header=header,
                            status_change=status_change, details=details, limit=limit)
        return text, self._keyboard(user, inv, details=details)

    def _archive_card(self, user: BotUser, inv: Investment) -> tuple[str, dict]:
        """Karta po końcu dostępu – tylko to, co osoba sama zapisała; bez szczegółów i bez akcji."""
        remind_at = self.store.reminder(user.chat_id, inv.id_sprawy)
        text = ui.archive_card(inv, flags=self.store.lead_flags(user.chat_id, inv.id_sprawy),
                               outcome=self.store.outcome(user.chat_id, inv.id_sprawy),
                               note=self.store.note(user.chat_id, inv.id_sprawy),
                               reminder_on=f"{local(remind_at):%d.%m}" if remind_at else None)
        return text, ui.archive_keyboard(inv)

    def _keyboard(self, user: BotUser, inv: Investment, view: str = "main", *, details: bool = False) -> dict:
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
            outcome=self.store.outcome(user.chat_id, inv.id_sprawy),
            details=details,
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
        text, markup = ui.saved_list(leads, page, self.store.saved_count(user.chat_id), distance)
        if self._level(user) < FULL:  # po końcu dostępu: własna praca do wglądu, bez nowych danych
            text = ui.archive_saved_head() + "\n\n" + text
        return text, markup

    def _place_names(self) -> dict[str, str]:
        return dict(self.store.place_options(self.powiat_codes))

    def _send(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        self.api.send_message(chat_id, text, markup)

    def _send_long(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        """Lista albo raport admina dłuższy niż limit Telegrama – w kilku wiadomościach (przyciski pod ostatnią)."""
        parts = ui.split_lines(text)
        for index, part in enumerate(parts, start=1):
            self._send(chat_id, part, markup if index == len(parts) else None)

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
    "mz": LeadBot._cb_place_confirm,
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
    "d": LeadBot._cb_details,
    "i": LeadBot._cb_intro,
    "zm": LeadBot._cb_order,
    "w": LeadBot._cb_outcome,
    "wp": LeadBot._cb_reason,
}
_CALLBACK_LEVELS: dict[str, int] = {
    **{prefix: SETUP for prefix in ("f", "fp", "fpr", "mz", "fr", "fb", "ft", "fv", "fi", "m", "ob", "oa")},
    "ts": NONE,
    "st": NONE,  # ekran ustawień sam sprawdza poziom dla każdego przycisku
    "o": NONE,  # bez dostępu: tylko własna praca w trybie archiwum (sprawdza handler)
    "i": NONE,  # opis produktu, przykład, oferta, prośba o test – dla każdego
    "zm": NONE,  # zamówienie i pytanie o ofertę – także po końcu dostępu
    "sv": NONE,
}
"""Poziom uprawnień przycisków (domyślnie pełny dostęp – inwestycje, zapisane, obserwowane)."""


def campaign_source(payload: str | None) -> str | None:
    """Źródło wejścia z parametru ``/start``: krótki kod kampanii (litery, cyfry, ``_``, ``-``; zaczyna się literą).

    Wszystko inne – za długie, z kropką, „@”, spacją albo z ciągiem 5+ cyfr (np. numer telefonu) – zapisujemy
    jako ``inne``: parametr kampanii nie może przemycić danych osobowych ani dowolnego tekstu.
    """
    if not payload:
        return None
    value = payload.strip().lower()
    if not _SOURCE_RE.fullmatch(value) or re.search(r"\d{5,}", value):
        return "inne"
    return value


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
