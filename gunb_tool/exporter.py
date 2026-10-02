"""Powiadomienia (Telegram, Discord) oraz eksport leadów do Google Sheets.

Treść wiadomości powstaje z jednego opisu i jest renderowana w dwóch formatach:

* Telegram – HTML (``parse_mode=HTML``): escapowane są wyłącznie ``& < >``, więc nazwy firm
  i adresy ze znakami specjalnymi nie psują wiadomości; linki trafiają do przycisków inline,
* Discord – Markdown z linkami w treści.

Gdy do wysłania jest więcej leadów niż ``digest_threshold``, zamiast serii wiadomości wysyłany jest
raport zbiorczy (dzielony na części mieszczące się w limicie długości wiadomości). Wysyłka jest
kolejkowana z limitem tempa (Telegram: najwyżej 1 wiadomość na sekundę).
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .http_client import HttpError, ResilientHttpClient
from .models import BUILDING_CATEGORIES, LEAD_CATEGORIES, Investment, Status
from .scoring import HOT, PRIORITY_BADGES, score_investment
from .storage import StatusChange

log = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_LIMIT = 4096
TELEGRAM_MIN_INTERVAL = 1.0
"""Minimalny odstęp między wiadomościami Telegrama (s) – limit API to ~1 wiadomość/s na czat."""
DISCORD_LIMIT = 2000
DISCORD_SUPPRESS_EMBEDS = 1 << 2

CATEGORY_ICONS: dict[str, str] = {
    "mieszkaniowa-jednorodzinna": "🏠",
    "mieszkaniowa-wielorodzinna": "🏢",
    "mieszana": "🏘️",
    "komercyjna": "🏭",
    "publiczna": "🏫",
    "rolnicza": "🌾",
    "inna": "🔧",
    "szum": "🚫",
}

_DISCORD_RESERVED_RE = re.compile(r"([\\*_~`|>\[\]])")
_DIGEST_HEADER_RESERVE = 120


class ExportError(RuntimeError):
    """Błąd eksportu lub wysyłki."""


class NotificationError(ExportError):
    """Nie udało się wysłać powiadomienia."""


class SheetsError(ExportError):
    """Błąd konfiguracji lub zapisu Google Sheets."""


@dataclass(frozen=True)
class OutgoingMessage:
    """Wiadomość gotowa do wysłania.

    Attributes:
        text: treść w formacie kanału (HTML dla Telegrama, Markdown dla Discorda).
        buttons: przyciski-linki ``(etykieta, url)``; Telegram pokazuje każdy w osobnym, pełnym wierszu.
        lead_ids: leady opisane wiadomością – po udanej wysyłce oznaczane jako wysłane.
    """

    text: str
    buttons: tuple[tuple[str, str], ...] = ()
    lead_ids: tuple[str, ...] = ()


# --- Escapowanie i style ---------------------------------------------------------------

def escape_html(text: str) -> str:
    """Escapuje tekst do Telegram HTML (``&``, ``<``, ``>``)."""
    return html.escape(text, quote=False)


def telegram_length(text: str) -> int:
    """Długość wiadomości tak, jak liczy ją Telegram przy limicie 4096: w jednostkach UTF-16 – większość emoji
    (🏠, 📊, 👍) to 2, polskie litery – 1. Liczona dla całego HTML, więc z zapasem: Telegram liczy sam tekst."""
    return len(text.encode("utf-16-le")) // 2


def escape_discord(text: str) -> str:
    """Escapuje znaki formatowania Markdown Discorda."""
    return _DISCORD_RESERVED_RE.sub(r"\\\1", text)


class _HtmlStyle:
    length = staticmethod(telegram_length)

    @staticmethod
    def text(value: str) -> str:
        return escape_html(value)

    @staticmethod
    def bold(value: str) -> str:
        return "<b>" + escape_html(value) + "</b>"

    @staticmethod
    def code(value: str) -> str:
        return "<code>" + escape_html(value) + "</code>"

    @staticmethod
    def link(label: str, url: str) -> str:
        return '<a href="' + html.escape(url, quote=True) + '">' + escape_html(label) + "</a>"


class _DiscordStyle:
    length = staticmethod(len)

    @staticmethod
    def text(value: str) -> str:
        return escape_discord(value)

    @staticmethod
    def bold(value: str) -> str:
        return "**" + escape_discord(value) + "**"

    @staticmethod
    def code(value: str) -> str:
        return "`" + value.replace("`", "'") + "`"

    @staticmethod
    def link(label: str, url: str) -> str:
        return "[" + escape_discord(label) + "](" + url + ")"


# --- Formatowanie wiadomości ----------------------------------------------------------

@dataclass(frozen=True)
class _Line:
    icon: str
    label: str
    value: str
    code: bool = False
    suffix: str = ""


class MessageFormatter:
    """Buduje wiadomości o pojedynczych leadach oraz raporty zbiorcze.

    Args:
        description_limit: maksymalna długość opisu zamierzenia (dłuższe są skracane „…”).
        segment_labels: czytelne nazwy segmentów klientów (``{"domki": "Domki jednorodzinne"}``).
    """

    def __init__(self, description_limit: int = 400, segment_labels: Mapping[str, str] | None = None) -> None:
        self.description_limit = description_limit
        self.segment_labels = dict(segment_labels or {})

    # Pojedyncze leady ------------------------------------------------------------------

    def telegram(
        self, investment: Investment, change: StatusChange | None = None, *, header: tuple[str, str, str] | None = None,
        limit: int = TELEGRAM_LIMIT,
    ) -> OutgoingMessage:
        """Wiadomość Telegram (HTML) z przyciskami „Otwórz w Google Maps” i „Geoportal”.

        ``header`` = ``(ikona, tytuł, opis)`` zastępuje domyślny nagłówek (np. alert watchlisty);
        ``limit`` – mniejszy niż limit Telegrama, gdy pod kartą coś jeszcze się dopisze (np. notatka).
        """
        text = self._fit(investment, change, _HtmlStyle, limit, links_in_text=False, header=header)
        return OutgoingMessage(text, buttons=_buttons(investment), lead_ids=(investment.id_sprawy,))

    def discord(
        self, investment: Investment, change: StatusChange | None = None, *, header: tuple[str, str, str] | None = None
    ) -> OutgoingMessage:
        """Wiadomość w Markdown Discorda (linki w treści)."""
        text = self._fit(investment, change, _DiscordStyle, DISCORD_LIMIT, links_in_text=True, header=header)
        return OutgoingMessage(text, lead_ids=(investment.id_sprawy,))

    # Raporty zbiorcze -----------------------------------------------------------------

    def telegram_digest(
        self, leads: Sequence[Investment], changes: Mapping[str, StatusChange | None] | None = None
    ) -> list[OutgoingMessage]:
        """Raport zbiorczy w Telegram HTML, podzielony na wiadomości ≤ 4096 znaków."""
        return self._digest(leads, changes or {}, _HtmlStyle, TELEGRAM_LIMIT)

    def discord_digest(
        self, leads: Sequence[Investment], changes: Mapping[str, StatusChange | None] | None = None
    ) -> list[OutgoingMessage]:
        """Raport zbiorczy w Markdown Discorda, podzielony na wiadomości ≤ 2000 znaków."""
        return self._digest(leads, changes or {}, _DiscordStyle, DISCORD_LIMIT)

    # Wewnętrzne ------------------------------------------------------------------------

    def _fit(
        self, inv: Investment, change: StatusChange | None, style: Any, limit: int, *,
        links_in_text: bool, header: tuple[str, str, str] | None = None,
    ) -> str:
        """Skraca opis, a w razie potrzeby każde pole – zawsze przed escapowaniem, więc HTML/Markdown zostaje cały."""
        text = ""
        for description_limit, field_limit in ((self.description_limit, 400), (200, 150), (80, 60), (40, 30)):
            text = self._render(inv, change, style, description_limit, links_in_text, header, field_limit)
            if style.length(text) <= limit:
                return text
        return text  # nieosiągalne dla rozsądnego limitu: ~20 krótkich wierszy

    def _render(
        self, inv: Investment, change: StatusChange | None, style: Any, description_limit: int,
        links_in_text: bool, header: tuple[str, str, str] | None = None, field_limit: int = 400,
    ) -> str:
        source = "pozwolenie na budowę" if inv.zrodlo == "pozwolenia" else "zgłoszenie budowy"
        if header is not None:
            icon, title, detail = header
        elif change is not None and change.stary_status:
            icon, title = "🔄", "ZMIANA STATUSU"
            detail = f"{_status_word(change.stary_status)} → {_status_word(change.nowy_status)}"
        elif inv.priorytet == HOT:
            icon, title, detail = "🔥", "HOT", source
        else:
            icon, title, detail = "🏗️", "NOWA INWESTYCJA", source

        lines = [
            f"{icon} {style.bold(title)} · {style.text(detail)}",
            style.bold(_truncate(inv.nazwa_zamierzenia or "(brak opisu zamierzenia)", description_limit)),
            "",
        ]
        for line in self._details(inv):
            raw = _truncate(line.value, field_limit)
            value = style.code(raw) if line.code else style.text(raw)
            if line.suffix:
                value += " " + style.text(line.suffix)
            lines.append(f"{line.icon} {style.text(line.label)}: {value}")
        if links_in_text:
            links = [(label, url) for label, url in (("Google Maps", inv.google_maps_url),
                                                     ("Geoportal", inv.geoportal_url)) if url]
            if links:
                lines += ["", " · ".join(style.link(label, url) for label, url in links)]
        return "\n".join(lines)

    def _details(self, inv: Investment) -> list[_Line]:
        lines = [_Line("📌", "Status", _status_label(inv.status))]
        if inv.priorytet in PRIORITY_BADGES:
            score = score_investment(inv)
            points = inv.punkty if inv.punkty is not None else score.points
            reasons = f" ({', '.join(score.reasons[:4])})" if score.reasons else ""
            # skala inwestycji z prostych reguł (kubatura, rodzaj, liczba budynków) – nie szansa na zlecenie
            lines.append(_Line("🎚️", "Skala (szacunek)", f"{PRIORITY_BADGES[inv.priorytet]} · {points} pkt{reasons}"))
        if inv.segment:
            lines.append(_Line("🎯", "Segment", self.segment_labels.get(inv.segment, inv.segment)))

        category = inv.kategoria or "inna"
        if inv.kategoria_obiektu:
            description = BUILDING_CATEGORIES.get(inv.kategoria_obiektu)
            category += f" · kat. {inv.kategoria_obiektu}" + (f" – {description}" if description else "")
        lines.append(_Line("🏷️", "Kategoria", category))

        if inv.adres_opisowy:
            lines.append(_Line("📍", "Adres", inv.adres_opisowy))
        location = ", ".join(p for p in (f"gm. {inv.gmina}" if inv.gmina else None, inv.powiat) if p)
        if inv.precyzja_geo == "obreb":
            location = f"{location} (lokalizacja przybliżona – środek obrębu)".strip()
        if location:
            lines.append(_Line("🗺️", "Lokalizacja", location))

        lines.append(_Line("💼", "Inwestor", inv.inwestor or "niejawny (osoba fizyczna lub brak w rejestrze)"))
        designer = ", ".join(dict.fromkeys(p for p in (inv.projektant, inv.pracownia) if p))
        if designer:
            if inv.projektant_uprawnienia:
                designer += f" (upr. {inv.projektant_uprawnienia})"
            lines.append(_Line("📐", "Projektant", designer))
        if inv.kubatura:
            lines.append(_Line("📦", "Kubatura", _volume(inv.kubatura)))

        dates = " · ".join(p for p in (
            f"decyzja {inv.data_decyzji}" if inv.data_decyzji else None,
            f"wpływ {inv.data_wplywu}" if inv.data_wplywu else None,
        ) if p)
        if dates:
            lines.append(_Line("📅", "Daty", dates))
        if inv.organ:
            lines.append(_Line("🏛️", "Organ", inv.organ))
        if inv.teryt_dzialki:
            extra = len(inv.dzialki) - 1
            lines.append(_Line("🧩", "Działka", inv.teryt_dzialki, code=True, suffix=f"(+{extra})" if extra > 0 else ""))
        lines.append(_Line("🔖", "Sprawa", inv.id_sprawy, code=True))
        return lines

    def _digest(
        self, leads: Sequence[Investment], changes: Mapping[str, StatusChange | None], style: Any, limit: int
    ) -> list[OutgoingMessage]:
        if not leads:
            return []
        newest_first = sorted(leads, key=lambda inv: inv.data_aktualizacji or "", reverse=True)
        ordered = sorted(newest_first, key=lambda inv: _category_rank(inv.kategoria))
        summary = self._digest_summary(leads, changes, style)
        budget = limit - _DIGEST_HEADER_RESERVE - style.length(summary)

        chunks: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        size = 0
        for inv in ordered:
            entry = self._digest_entry(inv, changes.get(inv.id_sprawy), style)
            if current and size + style.length(entry) + 2 > budget:
                chunks.append(current)
                current, size = [], 0
            current.append((inv.id_sprawy, entry))
            size += style.length(entry) + 2
        chunks.append(current)

        messages = []
        for number, chunk in enumerate(chunks, start=1):
            header = f"📊 {style.bold('Raport GUNB')} · {style.text(plural_leads(len(leads)))}"
            if len(chunks) > 1:
                header += f" ({number}/{len(chunks)})"
            parts = [header, *([summary] if number == 1 else []), *(entry for _, entry in chunk)]
            messages.append(OutgoingMessage("\n\n".join(parts), lead_ids=tuple(lead_id for lead_id, _ in chunk)))
        return messages

    def _digest_summary(self, leads: Sequence[Investment], changes: Mapping[str, StatusChange | None], style: Any) -> str:
        categories = Counter(inv.kategoria or "inna" for inv in leads)
        lines = [" · ".join(
            f"{CATEGORY_ICONS.get(name, '•')} {name}: {categories[name]}"
            for name in sorted(categories, key=_category_rank)
        )]
        segments = Counter(inv.segment for inv in leads if inv.segment)
        if segments:
            lines.append("🎯 " + " · ".join(f"{self.segment_labels.get(name, name)}: {count}"
                                             for name, count in segments.most_common()))
        changed = sum(1 for inv in leads if _is_status_change(changes.get(inv.id_sprawy)))
        if changed:
            lines.append(f"🔄 zmiany statusu: {changed}")
        return "\n".join(style.text(line) for line in lines)

    @staticmethod
    def _digest_entry(inv: Investment, change: StatusChange | None, style: Any) -> str:
        icon = ("🔥 " if inv.priorytet == HOT else "") + CATEGORY_ICONS.get(inv.kategoria or "inna", "•")
        lines = [f"{icon} {style.bold(_truncate(inv.nazwa_zamierzenia or '(brak opisu zamierzenia)', 110))}"]
        if _is_status_change(change):
            lines.append(style.text(f"🔄 {_status_word(change.stary_status)} → {_status_word(change.nowy_status)}"))
        event = (f"decyzja {inv.data_decyzji}" if inv.data_decyzji
                 else f"wpływ {inv.data_wplywu}" if inv.data_wplywu else None)
        facts = [_truncate(p, 90) for p in (inv.adres_opisowy or inv.miejscowosc,
                                            _volume(inv.kubatura) if inv.kubatura else None, event) if p]
        if facts:
            lines.append(style.text(" · ".join(facts)))
        refs = []
        if inv.google_maps_url:
            refs.append(style.link("📍 mapa", inv.google_maps_url))
        if inv.geoportal_url:
            refs.append(style.link("🏛️ działka", inv.geoportal_url))
        refs.append(style.code(inv.id_sprawy))
        lines.append(" · ".join(refs))
        return "\n".join(lines)


def plural_leads(count: int) -> str:
    """„1 inwestycja”, „3 inwestycje”, „11 inwestycji”, „22 inwestycje” – polska odmiana liczebnika."""
    if count == 1:
        word = "inwestycja"
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = "inwestycje"
    else:
        word = "inwestycji"
    return f"{count} {word}"


def _buttons(inv: Investment) -> tuple[tuple[str, str], ...]:
    buttons = []
    if inv.google_maps_url:
        buttons.append(("📍 Otwórz w Google Maps", inv.google_maps_url))
    if inv.geoportal_url:
        buttons.append(("🏛️ Geoportal", inv.geoportal_url))
    return tuple(buttons)


def _is_status_change(change: StatusChange | None) -> bool:
    return change is not None and change.stary_status is not None


def _category_rank(category: str | None) -> int:
    order = (*LEAD_CATEGORIES, "szum")
    return order.index(category) if category in order else len(order)


def _volume(kubatura: float) -> str:
    return format(kubatura, ",.0f").replace(",", " ") + " m³"


def _status_label(status: str) -> str:
    try:
        return Status(status).label
    except ValueError:
        return status


def _status_word(status: str) -> str:
    return status.replace("_", " ")


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


# --- Wysyłka ----------------------------------------------------------------------------

class RateLimiter:
    """Kolejkuje wywołania tak, by między kolejnymi upłynęło co najmniej ``min_interval`` sekund."""

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        """Czeka tyle, ile trzeba, by zachować odstęp od poprzedniego wywołania."""
        if self._last is not None:
            remaining = self.min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()

    def touch(self) -> None:
        """Zapamiętuje koniec operacji – kolejny odstęp liczony jest od tej chwili.

        Bez tego dłuższe pierwsze żądanie (np. nawiązanie TLS) „zjadało” część odstępu
        i serwer dostawał wiadomości częściej niż raz na ``min_interval``.
        """
        self._last = self._clock()


class Notifier(Protocol):
    """Kanał powiadomień."""

    channel: str

    def destination(self, segment: str | None) -> str: ...

    def send(self, message: OutgoingMessage, destination: str | None = None) -> None: ...


class TelegramNotifier:
    """Bot API ``sendMessage`` w trybie HTML, z przyciskami inline i limitem 1 wiadomość/s.

    Args:
        http: klient HTTP.
        bot_token: token od @BotFather.
        chat_id: domyślny czat/kanał.
        segment_chats: osobne czaty dla segmentów klientów (``{"domki": "-100…"}``).
        min_interval: odstęp między wiadomościami; wartości poniżej 1 s są podnoszone do 1 s.
    """

    channel = "telegram"

    def __init__(
        self,
        http: ResilientHttpClient,
        bot_token: str,
        chat_id: str,
        *,
        segment_chats: Mapping[str, str] | None = None,
        min_interval: float = TELEGRAM_MIN_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not bot_token or not chat_id:
            raise NotificationError(
                "Brak konfiguracji Telegrama – ustaw TELEGRAM_BOT_TOKEN i TELEGRAM_CHAT_ID (sekcja telegram)"
            )
        self.http = http
        self._url = TELEGRAM_API_URL.format(token=bot_token)
        self.chat_id = chat_id
        self.segment_chats = {name: chat for name, chat in (segment_chats or {}).items() if chat}
        self._limiter = RateLimiter(max(TELEGRAM_MIN_INTERVAL, min_interval), clock=clock, sleep=sleep)

    def destination(self, segment: str | None) -> str:
        """Czat dla leada z danego segmentu (domyślny, gdy segment nie ma własnego)."""
        return self.segment_chats.get(segment or "", self.chat_id)

    def send(self, message: OutgoingMessage, destination: str | None = None) -> None:
        """Wysyła wiadomość; rzuca :class:`NotificationError`, gdy Telegram ją odrzuci."""
        payload: dict[str, Any] = {
            "chat_id": destination or self.chat_id,
            "text": message.text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if message.buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": label, "url": url}]
                                                           for label, url in message.buttons]}
        self._limiter.wait()
        try:
            response = self.http.post(self._url, json=payload)
        except HttpError as exc:
            raise NotificationError(f"Telegram: {exc}") from exc
        finally:
            self._limiter.touch()
        body = _json(response)
        if response.status_code != 200 or not body.get("ok"):
            raise NotificationError(
                f"Telegram odrzucił wiadomość (HTTP {response.status_code}): {body.get('description', '')}"
            )


class DiscordNotifier:
    """Webhook Discorda (bez wzmianek @everyone i bez podglądów linków), z limitem tempa."""

    channel = "discord"

    def __init__(
        self,
        http: ResilientHttpClient,
        webhook_url: str,
        *,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not webhook_url:
            raise NotificationError("Brak konfiguracji Discorda – ustaw DISCORD_WEBHOOK_URL (sekcja discord)")
        self.http = http
        self._url = webhook_url
        self._limiter = RateLimiter(min_interval, clock=clock, sleep=sleep)

    def destination(self, segment: str | None) -> str:
        """Discord ma jeden webhook – wszystkie segmenty trafiają na ten sam kanał."""
        return "discord"

    def send(self, message: OutgoingMessage, destination: str | None = None) -> None:
        """Wysyła wiadomość; rzuca :class:`NotificationError`, gdy Discord ją odrzuci."""
        payload = {"content": message.text, "allowed_mentions": {"parse": []}, "flags": DISCORD_SUPPRESS_EMBEDS}
        self._limiter.wait()
        try:
            response = self.http.post(self._url, json=payload)
        except HttpError as exc:
            raise NotificationError(f"Discord: {exc}") from exc
        finally:
            self._limiter.touch()
        if response.status_code not in (200, 204):
            raise NotificationError(
                f"Discord odrzucił wiadomość (HTTP {response.status_code}): {_json(response).get('message', '')}"
            )


def _json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# --- Google Sheets -----------------------------------------------------------------------

SHEET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id_sprawy", "ID sprawy"),
    ("status", "Status"),
    ("data_aktualizacji", "Data aktualizacji"),
    ("kategoria", "Kategoria"),
    ("segment", "Segment"),
    ("kategoria_obiektu", "Kat. obiektu"),
    ("nazwa_zamierzenia", "Nazwa zamierzenia"),
    ("adres_opisowy", "Adres"),
    ("miejscowosc", "Miejscowość"),
    ("gmina", "Gmina"),
    ("powiat", "Powiat"),
    ("teryt_dzialki", "Działka (TERYT)"),
    ("lat", "Lat"),
    ("lon", "Lon"),
    ("google_maps_url", "Google Maps"),
    ("geoportal_url", "Geoportal"),
    ("inwestor", "Inwestor"),
    ("projektant", "Projektant"),
    ("projektant_uprawnienia", "Uprawnienia"),
    ("pracownia", "Pracownia"),
    ("kubatura", "Kubatura m³"),
    ("organ", "Organ"),
    ("data_wplywu", "Data wpływu"),
    ("data_decyzji", "Data decyzji"),
    ("numer_decyzji", "Nr decyzji"),
    ("zrodlo", "Źródło"),
    ("is_residential", "Mieszkaniowa"),
    ("is_commercial", "Komercyjna"),
    ("zmieniono", "Zaktualizowano"),
)
"""Kolumny arkusza: (pole leada, nagłówek). Pierwsza kolumna jest kluczem upsertu."""


@dataclass(frozen=True)
class SheetsSyncResult:
    """Liczba zaktualizowanych i dopisanych wierszy."""

    updated: int
    appended: int


class GoogleSheetsExporter:
    """Upsert leadów do arkusza Google (klucz: kolumna A = ``id_sprawy``).

    Args:
        worksheet_factory: funkcja zwracająca obiekt arkusza zgodny z ``gspread.Worksheet``
            (wywoływana dopiero, gdy jest co eksportować).
    """

    def __init__(self, worksheet_factory: Callable[[], Any]) -> None:
        self._worksheet_factory = worksheet_factory

    @classmethod
    def from_service_account(
        cls, service_account_file: Path | None, spreadsheet_id: str, worksheet: str
    ) -> GoogleSheetsExporter:
        """Tworzy eksporter korzystający z konta serwisowego Google (pakiet ``gspread``).

        Raises:
            SheetsError: brak pliku klucza lub identyfikatora arkusza.
        """
        if not spreadsheet_id:
            raise SheetsError("Brak sheets.spreadsheet_id – ustaw GOOGLE_SHEET_ID")
        if service_account_file is None or not Path(service_account_file).is_file():
            raise SheetsError(
                f"Brak pliku klucza konta serwisowego: {service_account_file} – ustaw GOOGLE_SERVICE_ACCOUNT_FILE"
            )

        def factory() -> Any:
            try:
                import gspread
            except ImportError as exc:  # pragma: no cover - gspread jest w requirements.txt
                raise SheetsError("Brak pakietu gspread – uruchom: pip install -r requirements.txt") from exc
            client = gspread.service_account(filename=str(service_account_file))
            spreadsheet = client.open_by_key(spreadsheet_id)
            try:
                return spreadsheet.worksheet(worksheet)
            except gspread.WorksheetNotFound:
                log.info("Tworzę zakładkę %r w arkuszu", worksheet)
                return spreadsheet.add_worksheet(title=worksheet, rows=1000, cols=len(SHEET_COLUMNS))

        return cls(factory)

    def export(self, investments: Sequence[Investment]) -> SheetsSyncResult:
        """Aktualizuje istniejące wiersze (po ``id_sprawy``) i dopisuje nowe – stała liczba wywołań API."""
        if not investments:
            return SheetsSyncResult(0, 0)
        worksheet = self._worksheet_factory()
        existing = worksheet.get_all_values()
        header = [label for _, label in SHEET_COLUMNS]
        last_column = _column_letter(len(header))

        updates: list[dict[str, Any]] = []
        if not existing or existing[0][: len(header)] != header:
            updates.append({"range": f"A1:{last_column}1", "values": [header]})
        row_numbers = {row[0]: number for number, row in enumerate(existing[1:], start=2) if row and row[0]}

        appended: list[list[Any]] = []
        updated = 0
        for investment in investments:
            row = [_sheet_value(getattr(investment, name)) for name, _ in SHEET_COLUMNS]
            number = row_numbers.get(investment.id_sprawy)
            if number is None:
                appended.append(row)
            else:
                updates.append({"range": f"A{number}:{last_column}{number}", "values": [row]})
                updated += 1

        if updates:
            worksheet.batch_update(updates, raw=True)
        if appended:
            worksheet.append_rows(appended, value_input_option="RAW")
        return SheetsSyncResult(updated, len(appended))


def _sheet_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "tak" if value else "nie"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _column_letter(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters
