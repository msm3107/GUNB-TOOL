"""Interaktywny bot Telegram dla ekip budowlanych – każdy użytkownik ma własne filtry, tryb i listy.

Bot działa jako jeden stale uruchomiony proces (``python main.py --bot``):

* odbiera wiadomości i kliknięcia przycisków (long polling ``getUpdates``),
* o ustalonych godzinach sam pobiera dane GUNB (``bot.fetch_times``); nieudane pobieranie
  (np. awaria serwera GUNB) ponawia co godzinę aż do skutku,
* alerty 👀 watchlisty i tryb „⚡ od razu” obsługuje co ``instant_every_minutes`` minut,
* raporty „🌅 rano” i „🌙 wieczorem” wysyła raz dziennie o ustawionych godzinach,
* codziennie rano przypomina „⏰ Kiedy dzwonić” – o budowach, które doszły do etapu branży klienta.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from . import bot_ui as ui
from .bot_store import BotStore, BotUser, UserFilters, investor_key, watch_match
from .bot_ui import BOT_COMMANDS, MENU_BUTTONS
from .config import BotConfig
from .exporter import TELEGRAM_LIMIT, MessageFormatter, escape_html
from .models import Investment
from .scoring import HOT
from .stages import LONGEST_WINDOW_DAYS, get_trade, is_due
from .storage import LeadRepository
from .telegram_api import TelegramApiError

log = logging.getLogger(__name__)

__all__ = ["LeadBot", "MENU_BUTTONS", "BOT_COMMANDS"]

_PRIORITY_RANK = {"hot": 0, "normal": 1, "low": 2}

FETCH_RETRY_JOB = "pobieranie_ponow"
"""Zadanie w ``bot_jobs`` z terminem ponowienia nieudanego pobierania (brak wpisu = nic do ponowienia)."""
FETCH_RETRY_AFTER = timedelta(hours=1)
STAGE_REMINDER_JOB = "przypomnienia_etap"

MENU_ACTIONS: dict[str, str] = {
    "📊 Co nowego?": "_show_news",
    "🔎 Filtry": "_show_filters",
    "⭐ Zapisane": "_show_saved",
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
    "/uzytkownicy": "_show_users",
}
ADMIN_COMMANDS: dict[str, str] = {
    "/aktywuj": "_cmd_activate",  # /aktywuj <chat_id> <liczba_dni>
    "/trial": "_cmd_trial",  # /trial <chat_id> – 3 dni za darmo
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
        fetcher: funkcja pobierająca dane GUNB (wywoływana o ``fetch_times``); zwrócone ``False``
            oznacza nieudane pobieranie – bot ponowi je za godzinę.
        clock: czas lokalny (harmonogram i daty w raportach).
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
        fetcher: Callable[[], bool | None] | None = None,
        clock: Callable[[], datetime] = datetime.now,
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
        self._clock = clock
        self._offset: int | None = None

    # === Pętla główna ==========================================================================

    def setup(self) -> None:
        """Rejestruje menu komend widoczne pod „/” w Telegramie."""
        self.api.set_my_commands(BOT_COMMANDS)

    def run_forever(self, *, should_stop: Callable[[], bool] = lambda: False,
                    sleep: Callable[[float], None] = time.sleep) -> None:
        """Pętla bota: odbieranie aktualizacji na przemian z zadaniami harmonogramu."""
        self.setup()
        log.info("Bot uruchomiony – czekam na wiadomości (Ctrl+C kończy)")
        while not should_stop():
            try:
                self.poll_once(self.settings.poll_timeout)
            except TelegramApiError as exc:
                log.warning("Telegram: %s", exc)
                sleep(30 if exc.code == 409 else 5)  # 409 = drugi proces bota odbiera te same aktualizacje
            try:
                self.run_due_jobs()
            except Exception:  # zadanie nie może zatrzymać bota
                log.exception("Błąd zadania harmonogramu bota")

    def poll_once(self, timeout: int) -> int:
        """Pobiera i obsługuje oczekujące aktualizacje; zwraca ich liczbę."""
        if self._offset is None:
            stored = self.store.job_last_run("telegram_offset")
            self._offset = int(stored) if stored else None
        updates = self.api.get_updates(self._offset, timeout)
        for update in updates:
            try:
                self.handle_update(update)
            except Exception:  # błąd jednej wiadomości nie może zatrzymać bota
                log.exception("Błąd obsługi aktualizacji %s", update.get("update_id"))
            self._offset = int(update["update_id"]) + 1
            self.store.mark_job("telegram_offset", str(self._offset))
        return len(updates)

    def run_due_jobs(self) -> list[str]:
        """Uruchamia zaległe zadania: pobieranie GUNB, raporty rano/wieczorem, doręczanie „od razu”."""
        now = self._clock()
        ran: list[str] = []
        for hhmm in self.settings.fetch_times:
            name = f"pobieranie_{hhmm}"
            if self.fetcher is not None and self._due(name, now, hhmm):
                self.store.mark_job(name, _local_iso(now))
                log.info("Harmonogram: pobieranie danych GUNB (%s)", hhmm)
                self._fetch(now)
                ran.append(name)
        retry_at = self.store.job_last_run(FETCH_RETRY_JOB)
        if self.fetcher is not None and retry_at and datetime.fromisoformat(retry_at) <= now:
            log.info("Harmonogram: ponowienie nieudanego pobierania danych GUNB")
            self._fetch(now)
            ran.append(FETCH_RETRY_JOB)
        for name, hhmm, mode in (("raport_rano", self.settings.morning_time, "rano"),
                                 ("raport_wieczor", self.settings.evening_time, "wieczor")):
            if self._due(name, now, hhmm):
                self.store.mark_job(name, _local_iso(now))
                self.deliver_reports(mode)
                ran.append(name)
        if self._due(STAGE_REMINDER_JOB, now, self.settings.morning_time):
            self.store.mark_job(STAGE_REMINDER_JOB, _local_iso(now))
            self.deliver_stage_reminders()
            ran.append(STAGE_REMINDER_JOB)
        last = self.store.job_last_run("natychmiast")
        interval = timedelta(minutes=self.settings.instant_every_minutes)
        if ran or last is None or datetime.fromisoformat(last) + interval <= now:
            self.store.mark_job("natychmiast", _local_iso(now))
            if self.settings.access != "open":
                self.expire_subscriptions()  # co kilka minut – klient dowiaduje się o końcu abonamentu od razu
            self.deliver_instant()
            ran.append("natychmiast")
        return ran

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
        if user.status == "zablokowany":  # odblokował bota i znów pisze
            self.store.set_status(chat_id, "aktywny")
            user = self.store.get_user(chat_id)
        if user.status == "odrzucony":
            self._send(chat_id, ui.rejected_text())
            return
        if not self._has_access(user):  # bramkarz: bez opłaconego abonamentu żadne menu ani filtr nie działa
            self._send(chat_id, self._gate_text(user))
            return
        if message.get("location"):
            self._set_base(user, message["location"])
            return
        action = COMMAND_ACTIONS.get(command) if command else MENU_ACTIONS.get(text)
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
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id") or (callback.get("from") or {}).get("id")
        message_id = message.get("message_id")
        data = callback.get("data") or ""
        answer: str | None = None
        try:
            if data.startswith("adm:"):
                answer = self._admin_decision(chat_id, message_id, data)
            else:
                user = self.store.get_user(chat_id)
                if user is None or user.status == "odrzucony" or not self._has_access(user):
                    answer = "⛔ Brak aktywnego abonamentu – skontaktuj się z administratorem"
                else:
                    prefix, _, arg = data.partition(":")
                    handler = _CALLBACKS.get(prefix)
                    answer = handler(self, user, arg, message_id) if handler else None
        finally:
            self.api.answer_callback_query(callback.get("id"), answer)

    # === Rejestracja i admin ===================================================================

    def _start(self, chat_id: int, sender: dict[str, Any]) -> None:
        """Rejestracja: nowa osoba zapisuje się jako nieaktywna (bez abonamentu), admin dostaje jej kartę."""
        existed = self.store.get_user(chat_id) is not None
        user = self.store.register(chat_id, sender.get("first_name"), sender.get("username"), status="aktywny",
                                   backlog_days=self.settings.welcome_backlog_days)
        if user.status in ("zablokowany", "oczekuje"):
            self.store.set_status(chat_id, "aktywny")
            user = self.store.get_user(chat_id)
        if user.status == "odrzucony":
            self._send(chat_id, ui.rejected_text())
        elif self._has_access(user):
            self._send(chat_id, ui.welcome_text(user.imie), ui.menu_keyboard())
        else:
            self._send(chat_id, self._gate_text(user))
            if not existed:
                text, markup = ui.new_user_card(user)
                for admin in self.settings.admins:
                    self._send_safely(admin, text, markup)

    def _admin_decision(self, chat_id: int, message_id: int, data: str) -> str:
        """Przyciski z karty nowej osoby: 🎁 trial, ✅ 30 dni, ⛔ odrzuć."""
        if chat_id not in self.settings.admins:
            return "⛔ Tylko administrator może to zrobić"
        _, decision, target = data.split(":", 2)
        user = self.store.get_user(int(target)) if target.lstrip("-").isdigit() else None
        if user is None:
            return "Nie ma takiej osoby"
        if decision in ("ok", "trial"):
            report = self._grant(user, days=ui.DEFAULT_PAID_DAYS, trial=decision == "trial")
            self.api.edit_message_text(chat_id, message_id, report)
            return "🎁 Trial włączony" if decision == "trial" else "✅ Abonament aktywny"
        self.store.set_status(user.chat_id, "odrzucony")
        self.store.set_subscription(user.chat_id, user.subscription_ends, active=False)
        self._send_safely(user.chat_id, ui.rejected_text())
        self.api.edit_message_text(chat_id, message_id, f"⛔ Odrzucono: {escape_html(user.display_name)}")
        return "⛔ Odrzucono"

    # === Abonament (paywall) ======================================================================

    def _cmd_activate(self, admin_chat: int, args: list[str]) -> None:
        """``/aktywuj <chat_id> <liczba_dni>`` – tylko admin."""
        if len(args) != 2 or not _is_chat_id(args[0]) or not args[1].isdigit() or not 1 <= int(args[1]) <= 3650:
            self._send(admin_chat, ui.admin_usage_text())
            return
        self._grant_by_id(admin_chat, int(args[0]), days=int(args[1]), trial=False)

    def _cmd_trial(self, admin_chat: int, args: list[str]) -> None:
        """``/trial <chat_id>`` – tylko admin: równo 3 dni od teraz."""
        if len(args) != 1 or not _is_chat_id(args[0]):
            self._send(admin_chat, ui.admin_usage_text())
            return
        self._grant_by_id(admin_chat, int(args[0]), days=ui.TRIAL_DAYS, trial=True)

    def _grant_by_id(self, admin_chat: int, chat_id: int, *, days: int, trial: bool) -> None:
        user = self.store.get_user(chat_id)
        if user is None:
            self._send(admin_chat, ui.admin_unknown_user_text(chat_id))
            return
        self._send(admin_chat, self._grant(user, days=days, trial=trial))

    def _grant(self, user: BotUser, *, days: int, trial: bool) -> str:
        """Włącza abonament i powiadamia klienta; zwraca potwierdzenie dla admina.

        Trial: równo 3 dni od teraz (nie skraca trwającego, dłuższego abonamentu). Płatny: N dni
        od teraz, a gdy abonament jeszcze trwa – przedłużenie od jego końca (klient nie traci dni).
        """
        now = self.repo.now()
        current = datetime.fromisoformat(user.subscription_ends) \
            if user.subscription_ends and user.has_subscription(_utc_iso(now)) else None
        if trial:
            ends = now + timedelta(days=ui.TRIAL_DAYS)
            if current is not None and current >= ends:
                return ui.trial_skipped_text(user, self._local_date(user.subscription_ends))
        else:
            ends = (current or now) + timedelta(days=days)
        ends_iso = _utc_iso(ends)
        self.store.set_subscription(user.chat_id, ends_iso, active=True)
        if user.status != "aktywny":  # np. wcześniej odrzucony albo „zablokowany” – admin daje nową szansę
            self.store.set_status(user.chat_id, "aktywny")
        ends_on = self._local_date(ends_iso)
        text = ui.trial_text(ends_on, user.imie) if trial else ui.activated_text(days, ends_on, user.imie)
        try:
            self._send(user.chat_id, text, ui.menu_keyboard())
            delivered = True
        except TelegramApiError as exc:
            log.warning("Nie udało się powiadomić %s o abonamencie: %s", user.chat_id, exc)
            delivered = False
        return ui.admin_granted_text(user, self._local_date(ends_iso, with_time=True), trial=trial, days=days,
                                     delivered=delivered)

    def expire_subscriptions(self) -> int:
        """Wyłącza abonamenty po terminie: klient dostaje jedno powiadomienie, admin – listę do przedłużenia."""
        expired = self.store.expired_subscriptions(_utc_iso(self.repo.now()))
        entries = []
        for user in expired:
            self.store.set_subscription(user.chat_id, user.subscription_ends, active=False)
            ends_on = self._local_date(user.subscription_ends)
            self._send_safely(user.chat_id, ui.gate_text(self._contact_html(), expired_on=ends_on))
            entries.append((user, ends_on))
        if entries:
            for admin in self.settings.admins:
                self._send_safely(admin, ui.admin_expired_text(entries))
        return len(entries)

    def _has_access(self, user: BotUser) -> bool:
        return (user.chat_id in self.settings.admins or self.settings.access == "open"
                or user.has_subscription(_utc_iso(self.repo.now())))

    def _gate_text(self, user: BotUser) -> str:
        expired_on = self._local_date(user.subscription_ends) if user.subscription_ends else None
        return ui.gate_text(self._contact_html(), expired_on=expired_on)

    def _contact_html(self) -> str:
        return ui.admin_contact_html(self.settings.admin_contact, self.settings.admins)

    def _subscribers(self, tryb: str | None = None) -> list[BotUser]:
        """Odbiorcy pętli wysyłkowych: z bazy tylko osoby z aktywnym abonamentem (i admini)."""
        if self.settings.access == "open":
            return self.store.users(tryb=tryb)
        return self.store.subscribers(_utc_iso(self.repo.now()), admins=self.settings.admins, tryb=tryb)

    def _local_date(self, utc_iso: str | None, *, with_time: bool = False) -> str:
        """Data z bazy (UTC) w czasie lokalnym bota – do komunikatów (np. „29.10.2026”)."""
        if not utc_iso:
            return "—"
        offset = self._clock() - self.repo.now().replace(tzinfo=None)
        moment = datetime.fromisoformat(utc_iso).replace(tzinfo=None) + timedelta(minutes=round(offset.total_seconds() / 60))
        return moment.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")

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
        self._send(user.chat_id, *ui.nearby_screen(filters))

    def _show_help(self, user: BotUser) -> None:
        self._send(user.chat_id, ui.help_text(self.settings, admin=user.chat_id in self.settings.admins),
                   ui.menu_keyboard())

    def _show_users(self, user: BotUser) -> None:
        if user.chat_id not in self.settings.admins:
            self._send(user.chat_id, ui.unknown_text(), ui.menu_keyboard())
            return
        everyone = [u for status in ("aktywny", "oczekuje", "zablokowany", "odrzucony") for u in self.store.users(status)]
        self._send(user.chat_id, ui.users_list(everyone, self._subscription_label))

    def _subscription_label(self, user: BotUser) -> str:
        """Stan abonamentu na liście admina: „admin”, „do 29.10.2026”, „wygasł 02.10.2026”, „nieaktywny”."""
        if user.chat_id in self.settings.admins:
            return "👑 admin"
        if user.has_subscription(_utc_iso(self.repo.now())):
            return f"💳 do {self._local_date(user.subscription_ends)}"
        if user.subscription_ends:
            return f"⌛ wygasł {self._local_date(user.subscription_ends)}"
        return "nieaktywny"

    # === Kliknięcia: lead ======================================================================

    def _cb_open(self, user: BotUser, arg: str, message_id: int) -> str | None:
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono leada"
        self._send_card(user, inv)
        return None

    def _cb_state(self, user: BotUser, arg: str, message_id: int, state: str, toast: str) -> str:
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono leada"
        self.store.set_lead_state(user.chat_id, inv.id_sprawy, state)
        self._refresh_keyboard(user, inv, message_id)
        return toast

    def _cb_save(self, user: BotUser, arg: str, message_id: int) -> str:
        return self._cb_state(user, arg, message_id, "zapisany", "⭐ Zapisano – znajdziesz go pod „⭐ Zapisane”")

    def _cb_reviewed(self, user: BotUser, arg: str, message_id: int) -> str:
        return self._cb_state(user, arg, message_id, "przejrzany", "✅ Oznaczono jako przejrzany")

    def _cb_hide(self, user: BotUser, arg: str, message_id: int) -> str:
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono leada"
        self.store.set_lead_state(user.chat_id, inv.id_sprawy, "ukryty")
        text, markup = ui.hidden_card(inv)
        self.api.edit_message_text(user.chat_id, message_id, text, markup)
        return "🗑️ Ukryto"

    def _cb_unhide(self, user: BotUser, arg: str, message_id: int) -> str:
        inv = self._lead(arg)
        if inv is None:
            return "Nie znaleziono leada"
        self.store.clear_lead_state(user.chat_id, inv.id_sprawy)
        text, markup = self._card(user, inv)
        self.api.edit_message_text(user.chat_id, message_id, text, markup)
        return "↩️ Przywrócono"

    def _cb_watch_investor(self, user: BotUser, arg: str, message_id: int) -> str:
        inv = self._lead(arg)
        key = investor_key(inv.inwestor) if inv else None
        if inv is None or key is None:
            return "Ten lead nie ma jawnego inwestora"
        return self._toggle_watch(user, inv, "inwestor", key, inv.inwestor or key, message_id)

    def _cb_watch_gmina(self, user: BotUser, arg: str, message_id: int) -> str:
        inv = self._lead(arg)
        if inv is None or not inv.gmina_teryt:
            return "Brak gminy dla tego leada"
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
        self._refresh_keyboard(user, inv, message_id)
        return toast

    # === Kliknięcia: filtry, tryb, listy ===========================================================

    def _cb_filters(self, user: BotUser, arg: str, message_id: int) -> str | None:
        if arg == "clear":
            self.store.set_filters(user.chat_id, UserFilters(baza=user.filtry.baza))  # bazę firmy pamiętamy
            self._edit_filters(user, message_id)
            return "🧹 Filtry wyczyszczone"
        if arg == "go":
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
        if trade is None or trade.months is None:
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

    def _text_input(self, user: BotUser, text: str) -> None:
        what = user.oczekuje_na
        self.store.set_awaiting(user.chat_id, None)
        filters = user.filtry
        value = " ".join(text.split())[:60]
        if what == "miejsce":
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
        """Alerty watchlisty (wszyscy) i leady trybu „⚡ od razu”; zwraca liczbę wysłanych wiadomości."""
        sent = 0
        for user in self._subscribers():
            try:
                sent += self._deliver_instant_to(user)
            except TelegramApiError as exc:
                self._delivery_failed(user, exc)
        return sent

    def deliver_reports(self, mode: str) -> int:
        """Raporty zbiorcze dla użytkowników w trybie ``mode`` (``rano``/``wieczor``)."""
        sent = 0
        for user in self._subscribers(tryb=mode):
            try:
                sent += int(self.send_report(user))
            except TelegramApiError as exc:
                self._delivery_failed(user, exc)
        return sent

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
        recent: list[Investment] = []
        if not leads:
            date_from = (self._clock().date() - timedelta(days=self.settings.recent_days)).isoformat()
            recent = _ranked([inv for inv in self.store.recent_leads(user.chat_id, date_from) if self._wanted(user, inv)],
                             nearest)

        def build(count: int) -> tuple[str, dict | None]:
            return ui.report(
                f"{self._clock():%d.%m}", total_new=len(candidates), leads=leads[:count], matching=len(leads),
                hot=sum(1 for inv in leads if inv.priorytet == HOT), watched=watched,
                recent=recent[:count], recent_total=len(recent), recent_days=self.settings.recent_days,
                distance=distance,
            )

        shown = min(len(leads or recent), self.settings.max_leads_in_report)
        text, markup = build(shown)
        while len(text) > TELEGRAM_LIMIT and shown > 1:  # długie opisy/adresy – mniej pozycji na liście
            shown = max(1, shown - 3)
            text, markup = build(shown)
        self._send(user.chat_id, text, markup)
        self.store.record_delivery(user.chat_id, leads[:shown], "raport")
        self.store.record_delivery(user.chat_id, skipped, "pominiety")
        self.store.mark_report(user.chat_id, now_iso)
        return True

    def deliver_stage_reminders(self) -> int:
        """Poranne przypomnienia „⏰ Kiedy dzwonić” dla osób z wybraną branżą; zwraca liczbę wiadomości."""
        sent = 0
        for user in self._subscribers():
            if user.branza is None:
                continue
            try:
                sent += int(self.send_stage_reminder(user))
            except TelegramApiError as exc:
                self._delivery_failed(user, exc)
        return sent

    def send_stage_reminder(self, user: BotUser, *, on_demand: bool = False) -> bool:
        """Budowy (zgodne z filtrami), które dziś są na etapie branży użytkownika – każda raz na branżę.

        Pokazane pozycje są zapisywane z rewizją ``etap:<branża>``; reszta (ponad limit listy) przyjdzie
        kolejnego ranka, bo okna etapów trwają tygodniami.
        """
        trade = get_trade(user.branza)
        if trade is None or trade.months is None:
            return False
        today = self._clock().date()
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
        sent, remaining = 0, []
        for inv in candidates:
            hit = watch_match(inv, watch_items) if watch_items else None
            if hit is None:
                remaining.append(inv)
                continue
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
        message = self.formatter.telegram(inv, self.repo.last_status_change(inv.id_sprawy), header=header)
        text = message.text
        km = user.filtry.distance_km(inv)
        if km is not None:
            text += f"\n🚗 {escape_html(ui.distance_label(km))} od Twojej bazy"
        return text, self._keyboard(user, inv)

    def _keyboard(self, user: BotUser, inv: Investment) -> dict:
        items = self.store.watchlist(user.chat_id)
        key = investor_key(inv.inwestor)
        return ui.lead_keyboard(
            inv,
            state=self.store.lead_state(user.chat_id, inv.id_sprawy),
            watching_investor=any(i.rodzaj == "inwestor" and i.wartosc == key for i in items) if key else False,
            watching_gmina=any(i.rodzaj == "gmina" and i.wartosc == inv.gmina_teryt for i in items),
        )

    def _send_card(self, user: BotUser, inv: Investment, header: tuple[str, str, str] | None = None) -> None:
        text, markup = self._card(user, inv, header)
        self._send(user.chat_id, text, markup)

    def _refresh_keyboard(self, user: BotUser, inv: Investment, message_id: int) -> None:
        self.api.edit_message_reply_markup(user.chat_id, message_id, self._keyboard(user, inv))

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

    def _fetch(self, now: datetime) -> None:
        """Pobiera dane GUNB; po nieudanej próbie planuje ponowienie za godzinę."""
        if self.fetcher is None:
            return
        try:
            succeeded = self.fetcher() is not False
        except Exception:  # np. zablokowana baza – bot działa dalej, pobieranie wróci za godzinę
            log.exception("Pobieranie danych GUNB przerwane nieoczekiwanym błędem – ponowię za godzinę")
            succeeded = False
        if succeeded:
            self.store.clear_job(FETCH_RETRY_JOB)
        else:
            self.store.mark_job(FETCH_RETRY_JOB, _local_iso(now + FETCH_RETRY_AFTER))

    def _due(self, name: str, now: datetime, hhmm: str) -> bool:
        hour, minute = (int(part) for part in hhmm.split(":"))
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < scheduled:
            return False
        last = self.store.job_last_run(name)
        return last is None or last < _local_iso(scheduled)


_CALLBACKS: dict[str, Callable[..., str | None]] = {
    "o": LeadBot._cb_open,
    "s": LeadBot._cb_save,
    "r": LeadBot._cb_reviewed,
    "h": LeadBot._cb_hide,
    "u": LeadBot._cb_unhide,
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
}


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


def _local_iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()
