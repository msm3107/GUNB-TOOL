"""Wspólne atrapy i pomocniki testów bota: zegar, atrapa API Telegrama, wiadomości i kliknięcia."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gunb_tool.bot import LeadBot
from gunb_tool.bot_store import BotStore
from gunb_tool.config import BotConfig
from gunb_tool.exporter import MessageFormatter
from gunb_tool.models import Investment
from gunb_tool.telegram_api import TelegramApiError

ADMIN, MIETEK, OBCY = 1001, 2002, 3003


class Clock:
    """Wspólny zegar bazy i bota (UTC); czas polski liczy bot (strefa Europe/Warsaw)."""

    def __init__(self) -> None:
        self.utc = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)

    def now_utc(self) -> datetime:
        return self.utc

    def advance(self, **delta) -> None:
        self.utc += timedelta(**delta)


class FakeApi:
    """Rejestruje wywołania API Telegrama; ``blocked`` = czaty, które zablokowały bota.

    ``fail(chat_id, *errors)`` – kolejne wysyłki do czatu kończą się podanymi wyjątkami; z ``delivered=True``
    wiadomość dochodzi, a mimo to leci wyjątek (np. timeout po przyjęciu wiadomości przez Telegram).
    """

    def __init__(self) -> None:
        self.sent, self.edits, self.answers, self.commands = [], [], [], []
        self.blocked: set[int] = set()
        self.failures: dict[int, list[tuple[BaseException, bool]]] = {}
        self._message_id = 500

    def fail(self, chat_id, *errors: BaseException, delivered: bool = False) -> None:
        self.failures.setdefault(chat_id, []).extend((error, delivered) for error in errors)

    def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.blocked:
            raise TelegramApiError("sendMessage", 403, "Forbidden: bot was blocked by the user")
        failure = self.failures[chat_id].pop(0) if self.failures.get(chat_id) else None
        if failure is not None and not failure[1]:
            raise failure[0]
        self._message_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup, "message_id": self._message_id})
        if failure is not None:
            raise failure[0]
        return {"message_id": self._message_id}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "markup": reply_markup})

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": None, "markup": reply_markup})

    def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append(text)

    def set_my_commands(self, commands):
        self.commands = commands

    # pomocnicze dla testów
    def last_to(self, chat_id):
        return [m for m in self.sent if m["chat_id"] == chat_id][-1]

    def to(self, chat_id):
        return [m for m in self.sent if m["chat_id"] == chat_id]


def buttons(markup):
    """Płaska lista (tekst, callback_data | url) z klawiatury inline."""
    return [(b["text"], b.get("callback_data") or b.get("url")) for row in (markup or {}).get("inline_keyboard", [])
            for b in row]


def callback_for(markup, text_fragment):
    return next(data for text, data in buttons(markup) if text_fragment in text)


def make_bot(repo, api, clock=None, **settings):
    """Bot na wspólnym zegarze bazy (``repo.now`` – UTC)."""
    params = dict(admins=(ADMIN,), access="approval", fetch_times=(), morning_time="07:00", evening_time="19:00",
                  instant_every_minutes=10, welcome_backlog_days=7, max_leads_in_report=20,
                  admin_contact="@admin_gunb")
    params.update(settings)
    return LeadBot(repo, api, settings=BotConfig(**params), powiat_codes=("1465", "3021"),
                   formatter=MessageFormatter())


def message(chat_id, text, first_name="Mietek"):
    return {"update_id": 1, "message": {"message_id": 1, "text": text,
                                        "chat": {"id": chat_id, "type": "private"},
                                        "from": {"id": chat_id, "first_name": first_name, "username": None}}}


def click(chat_id, data, message_id=777):
    return {"update_id": 2, "callback_query": {"id": "cb", "data": data, "from": {"id": chat_id},
                                               "message": {"message_id": message_id, "chat": {"id": chat_id}}}}


def lead(id_sprawy, **overrides) -> Investment:
    base = dict(
        id_sprawy=id_sprawy, zrodlo="pozwolenia", status="decyzja", kategoria="mieszkaniowa-jednorodzinna",
        nazwa_zamierzenia="Budowa budynku mieszkalnego jednorodzinnego", kubatura=878.0, priorytet="normal",
        punkty=3, adres_opisowy="Wróblewo", miejscowosc="Wróblewo", gmina="Kostrzyn", powiat="powiat poznański",
        powiat_teryt="3021", gmina_teryt="3021085", data_aktualizacji="2026-09-28",
        google_maps_url="https://www.google.com/maps?q=52.379110,17.211380",
    )
    base.update(overrides)
    return Investment(**base)


def configured(bot, chat_id=MIETEK):
    """Osoba ma za sobą pierwsze kroki (branża, obszar) – testy innych funkcji ich nie przechodzą."""
    BotStore(bot.repo).set_setup_step(chat_id, "gotowe")


def activate(bot, api, chat_id=MIETEK):
    """Rejestracja, pierwsze kroki za sobą i przycisk admina „✅ 30 dni”; liczniki atrapy wyczyszczone."""
    bot.handle_update(message(chat_id, "/start"))
    configured(bot, chat_id)
    if chat_id != ADMIN:
        bot.handle_update(click(ADMIN, f"adm:ok:{chat_id}"))
    api.sent.clear()
    api.edits.clear()
    api.answers.clear()
