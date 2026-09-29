"""Interaktywny bot Telegram dla ekip budowlanych – każdy użytkownik ma własne filtry, tryb i listy.

Bot działa jako jeden stale uruchomiony proces (``python main.py --bot``):

* odbiera wiadomości i kliknięcia przycisków (long polling ``getUpdates``),
* o ustalonych godzinach sam pobiera dane GUNB (``bot.fetch_times``),
* alerty 👀 watchlisty i tryb „⚡ od razu” obsługuje co ``instant_every_minutes`` minut,
* raporty „🌅 rano” i „🌙 wieczorem” wysyła raz dziennie o ustawionych godzinach.
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
from .exporter import MessageFormatter, escape_html
from .models import Investment
from .scoring import HOT
from .storage import LeadRepository
from .telegram_api import TelegramApiError

log = logging.getLogger(__name__)

__all__ = ["LeadBot", "MENU_BUTTONS", "BOT_COMMANDS"]

_PRIORITY_RANK = {"hot": 0, "normal": 1, "low": 2}

MENU_ACTIONS: dict[str, str] = {
    "📊 Co nowego?": "_show_news",
    "🔎 Filtry": "_show_filters",
    "⭐ Zapisane": "_show_saved",
    "👀 Obserwowane": "_show_watchlist",
    "⏰ Kiedy wysyłać": "_show_mode",
    "🔥 Tylko HOT": "_toggle_hot",
}
COMMAND_ACTIONS: dict[str, str] = {
    "/nowe": "_show_news",
    "/filtry": "_show_filters",
    "/zapisane": "_show_saved",
    "/obserwowane": "_show_watchlist",
    "/tryb": "_show_mode",
    "/tylkohot": "_toggle_hot",
    "/pomoc": "_show_help",
    "/uzytkownicy": "_show_users",
}


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
        fetcher: funkcja pobierająca dane GUNB (wywoływana o ``fetch_times``).
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
        fetcher: Callable[[], object] | None = None,
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
                self.fetcher()
                ran.append(name)
        for name, hhmm, mode in (("raport_rano", self.settings.morning_time, "rano"),
                                 ("raport_wieczor", self.settings.evening_time, "wieczor")):
            if self._due(name, now, hhmm):
                self.store.mark_job(name, _local_iso(now))
                self.deliver_reports(mode)
                ran.append(name)
        last = self.store.job_last_run("natychmiast")
        interval = timedelta(minutes=self.settings.instant_every_minutes)
        if ran or last is None or datetime.fromisoformat(last) + interval <= now:
            self.store.mark_job("natychmiast", _local_iso(now))
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
        user = self.store.get_user(chat_id)
        if command == "/start" or user is None:
            self._start(chat_id, message.get("from") or {})
            return
        if user.status == "zablokowany":  # odblokował bota i znów pisze
            self.store.set_status(chat_id, "aktywny")
            user = self.store.get_user(chat_id)
        if user.status != "aktywny":
            self._send(chat_id, ui.pending_text() if user.status == "oczekuje" else ui.rejected_text())
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
                if user is None or user.status != "aktywny":
                    answer = "⛔ Brak dostępu – napisz /start"
                else:
                    prefix, _, arg = data.partition(":")
                    handler = _CALLBACKS.get(prefix)
                    answer = handler(self, user, arg, message_id) if handler else None
        finally:
            self.api.answer_callback_query(callback.get("id"), answer)

    # === Rejestracja i admin ===================================================================

    def _start(self, chat_id: int, sender: dict[str, Any]) -> None:
        existed = self.store.get_user(chat_id) is not None
        is_admin = chat_id in self.settings.admins
        status = "aktywny" if is_admin or self.settings.access == "open" else "oczekuje"
        user = self.store.register(chat_id, sender.get("first_name"), sender.get("username"), status=status,
                                   backlog_days=self.settings.welcome_backlog_days)
        if user.status == "zablokowany" or (is_admin and user.status != "aktywny"):
            self.store.set_status(chat_id, "aktywny")
            user = self.store.get_user(chat_id)
        if user.status == "aktywny":
            self._send(chat_id, ui.welcome_text(user.imie), ui.menu_keyboard())
        elif user.status == "oczekuje":
            self._send(chat_id, ui.pending_text())
            if not existed:
                text, markup = ui.admin_approval(user)
                for admin in self.settings.admins:
                    self._send_safely(admin, text, markup)
        else:
            self._send(chat_id, ui.rejected_text())

    def _admin_decision(self, chat_id: int, message_id: int, data: str) -> str:
        if chat_id not in self.settings.admins:
            return "⛔ Tylko administrator może to zrobić"
        _, decision, target = data.split(":", 2)
        user = self.store.get_user(int(target))
        if user is None:
            return "Nie ma takiej osoby"
        if decision == "ok":
            self.store.set_status(user.chat_id, "aktywny")
            self._send_safely(user.chat_id, "✅ Masz dostęp!\n\n" + ui.welcome_text(user.imie), ui.menu_keyboard())
            self.api.edit_message_text(chat_id, message_id, f"✅ Wpuszczono: {user.display_name}")
            return "✅ Wpuszczono"
        self.store.set_status(user.chat_id, "odrzucony")
        self._send_safely(user.chat_id, ui.rejected_text())
        self.api.edit_message_text(chat_id, message_id, f"⛔ Odrzucono: {user.display_name}")
        return "⛔ Odrzucono"

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

    def _show_help(self, user: BotUser) -> None:
        self._send(user.chat_id, ui.help_text(self.settings), ui.menu_keyboard())

    def _show_users(self, user: BotUser) -> None:
        if user.chat_id not in self.settings.admins:
            self._send(user.chat_id, ui.unknown_text(), ui.menu_keyboard())
            return
        everyone = [u for status in ("aktywny", "oczekuje", "zablokowany", "odrzucony") for u in self.store.users(status)]
        self._send(user.chat_id, ui.users_list(everyone))

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
            self.store.set_filters(user.chat_id, UserFilters())
            self._edit_filters(user, message_id)
            return "🧹 Filtry wyczyszczone"
        screens = {"place": lambda f: ui.place_picker(f, self.store.place_options(self.powiat_codes)),
                   "type": ui.type_picker, "vol": ui.volume_picker, "inv": ui.investor_picker}
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
        return self._update_filters(user, replace(user.filtry, powiaty=powiaty), message_id, "place")

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
            filters = replace(filters, miejsca=tuple(dict.fromkeys((*filters.miejsca, value))))
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
        for user in self.store.users():
            try:
                sent += self._deliver_instant_to(user)
            except TelegramApiError as exc:
                self._delivery_failed(user, exc)
        return sent

    def deliver_reports(self, mode: str) -> int:
        """Raporty zbiorcze dla użytkowników w trybie ``mode`` (``rano``/``wieczor``)."""
        sent = 0
        for user in self.store.users(tryb=mode):
            try:
                sent += int(self.send_report(user))
            except TelegramApiError as exc:
                self._delivery_failed(user, exc)
        return sent

    def send_report(self, user: BotUser, *, on_demand: bool = False,
                    leads: Sequence[Investment] | None = None) -> bool:
        """Wysyła raport: podsumowanie + lista pasujących leadów z numerami do kliknięcia."""
        since = self._report_since(user)
        if leads is None:
            leads = [inv for inv in self.store.candidates(user.chat_id, since) if self._wanted(user, inv)]
        newest_first = sorted(leads, key=lambda i: i.data_aktualizacji or "", reverse=True)
        leads = sorted(newest_first, key=lambda i: (_PRIORITY_RANK.get(i.priorytet or "", 3), -(i.punkty or 0)))
        now_iso = _utc_iso(self.repo.now())
        if not leads and not on_demand:
            self.store.mark_report(user.chat_id, now_iso)
            return False
        shown = list(leads[: self.settings.max_leads_in_report])
        text, markup = ui.report(
            f"{self._clock():%d.%m}",
            total_new=self.store.count_new_since(since),
            leads=shown,
            matching=len(leads),
            hot=sum(1 for inv in leads if inv.priorytet == HOT),
            watched=self.store.deliveries_since(user.chat_id, since, "watchlista"),
        )
        self._send(user.chat_id, text, markup)
        self.store.record_delivery(user.chat_id, leads, "raport")
        self.store.mark_report(user.chat_id, now_iso)
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
        return message.text, self._keyboard(user, inv)

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
        return ui.saved_list(leads, page, self.store.saved_count(user.chat_id))

    def _place_names(self) -> dict[str, str]:
        return dict(self.store.place_options(self.powiat_codes))

    def _send(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        self.api.send_message(chat_id, text, markup)

    def _send_safely(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        try:
            self._send(chat_id, text, markup)
        except TelegramApiError as exc:
            log.warning("Nie udało się wysłać do %s: %s", chat_id, exc)

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
    "ft": LeadBot._cb_type,
    "fv": LeadBot._cb_volume,
    "fi": LeadBot._cb_investor,
    "m": LeadBot._cb_mode,
    "sv": LeadBot._cb_saved_page,
    "wd": LeadBot._cb_watch_delete,
}


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
