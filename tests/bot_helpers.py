"""Wspólne atrapy i pomocniki testów bota: zegar, atrapa API Telegrama, wiadomości i kliknięcia."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

from gunb_tool.bot import LeadBot
from gunb_tool.bot_store import BotStore
from gunb_tool.config import BotConfig
from gunb_tool.exporter import TELEGRAM_LIMIT, MessageFormatter
from gunb_tool.models import Investment
from gunb_tool.telegram_api import TelegramApiError

ADMIN, MIETEK, OBCY = 1001, 2002, 3003

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)>")
_TELEGRAM_TAGS = frozenset({"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre",
                            "blockquote", "tg-spoiler", "span", "tg-emoji"})
_ENTITY_RE = re.compile(r"&(?:lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);")


def telegram_rejects(text: str | None, markup: dict | None = None) -> str | None:
    """Dlaczego Telegram odrzuciłby tę wiadomość (``None`` – przyjmie): pusta albo dłuższa niż 4096 znaków,
    HTML spoza dozwolonych znaczników, niedomknięty albo z nieucieczkowanym ``<``/``>``/``&``, przycisk bez
    tekstu albo z ``callback_data`` dłuższym niż 64 bajty.

    Długość jak u Telegrama: tekst widoczny po odczytaniu HTML, w jednostkach UTF-16 – większość emoji to 2.
    """
    if text is not None:
        visible = html.unescape(_TAG_RE.sub("", text))
        if not visible.strip():
            return "message text is empty"
        length = len(visible.encode("utf-16-le")) // 2
        if length > TELEGRAM_LIMIT:
            return f"message is too long ({length} UTF-16 > {TELEGRAM_LIMIT})"
        stack: list[str] = []
        for match in _TAG_RE.finditer(text):
            closing, name = match.group(1), match.group(2).lower()
            if name not in _TELEGRAM_TAGS:
                return f"can't parse entities: unsupported start tag {name!r}"
            if not closing:
                stack.append(name)
            elif not stack or stack.pop() != name:
                return f"can't parse entities: unmatched end tag {name!r}"
        if stack:
            return f"can't parse entities: unclosed tag {stack[-1]!r}"
        rest = _ENTITY_RE.sub("", _TAG_RE.sub("", text))
        if "<" in rest or ">" in rest or "&" in rest:
            return "can't parse entities: unescaped <, > or &"
    for row in (markup or {}).get("inline_keyboard", []):
        for button in row:
            data = button.get("callback_data")
            if not button.get("text"):
                return "inline keyboard button text is empty"
            if data is None and not button.get("url"):
                return "inline keyboard button without callback_data or url"
            if data is not None and not 1 <= len(data.encode("utf-8")) <= 64:
                return f"BUTTON_DATA_INVALID: {data!r}"
    for row in (markup or {}).get("keyboard", []):
        if any(not button.get("text") for button in row):
            return "keyboard button text is empty"
    return None


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
        self.rejected: list[str] = []
        """Wiadomości, które prawdziwy Telegram by odrzucił (:func:`telegram_rejects`) – fikstura ``api`` pilnuje,
        żeby lista była pusta, także gdy bot połknie błąd (np. ``_send_safely``)."""
        self._message_id = 500

    def fail(self, chat_id, *errors: BaseException, delivered: bool = False) -> None:
        self.failures.setdefault(chat_id, []).extend((error, delivered) for error in errors)

    def _validate(self, method: str, text: str | None, markup) -> None:
        problem = telegram_rejects(text, markup)
        if problem is not None:
            self.rejected.append(f"{method}: {problem} – {(text or '')[:80]!r}")
            raise TelegramApiError(method, 400, f"Bad Request: {problem}")

    def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.blocked:
            raise TelegramApiError("sendMessage", 403, "Forbidden: bot was blocked by the user")
        failure = self.failures[chat_id].pop(0) if self.failures.get(chat_id) else None
        if failure is not None and not failure[1]:
            raise failure[0]
        self._validate("sendMessage", text, reply_markup)
        self._message_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup, "message_id": self._message_id})
        if failure is not None:
            raise failure[0]
        return {"message_id": self._message_id}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self._validate("editMessageText", text, reply_markup)
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "markup": reply_markup})

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        self._validate("editMessageReplyMarkup", None, reply_markup)
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
