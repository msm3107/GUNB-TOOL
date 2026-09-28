"""Powiadomienia (Telegram, Discord) oraz eksport leadów do Google Sheets.

Wiadomości są budowane z jednego opisu treści i renderowane w dwóch odmianach Markdown:
Telegram ``MarkdownV2`` (wymaga escapowania znaków zastrzeżonych) oraz Markdown Discorda.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .http_client import HttpError, ResilientHttpClient
from .models import BUILDING_CATEGORIES, Investment, Status
from .storage import StatusChange

log = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_LIMIT = 4096
DISCORD_LIMIT = 2000
DISCORD_SUPPRESS_EMBEDS = 1 << 2

_MDV2_RESERVED_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_DISCORD_RESERVED_RE = re.compile(r"([\\*_~`|>\[\]])")


class ExportError(RuntimeError):
    """Błąd eksportu lub wysyłki."""


class NotificationError(ExportError):
    """Nie udało się wysłać powiadomienia."""


class SheetsError(ExportError):
    """Błąd konfiguracji lub zapisu Google Sheets."""


# --- Escapowanie ------------------------------------------------------------------

def escape_markdown_v2(text: str) -> str:
    """Escapuje znaki zastrzeżone Telegram MarkdownV2 w zwykłym tekście."""
    return _MDV2_RESERVED_RE.sub(r"\\\1", text)


def escape_discord(text: str) -> str:
    """Escapuje znaki formatowania Markdown Discorda."""
    return _DISCORD_RESERVED_RE.sub(r"\\\1", text)


class _TelegramStyle:
    @staticmethod
    def text(value: str) -> str:
        return escape_markdown_v2(value)

    @staticmethod
    def bold(value: str) -> str:
        return "*" + escape_markdown_v2(value) + "*"

    @staticmethod
    def code(value: str) -> str:
        return "`" + value.replace("\\", "\\\\").replace("`", "\\`") + "`"

    @staticmethod
    def link(label: str, url: str) -> str:
        return "[" + escape_markdown_v2(label) + "](" + url.replace("\\", "\\\\").replace(")", "\\)") + ")"


class _DiscordStyle:
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


class MessageFormatter:
    """Buduje czytelne wiadomości o leadach.

    Args:
        description_limit: maksymalna długość opisu zamierzenia (dłuższe są skracane „…”).
    """

    def __init__(self, description_limit: int = 400) -> None:
        self.description_limit = description_limit

    def telegram(self, investment: Investment, change: StatusChange | None = None) -> str:
        """Wiadomość w formacie Telegram MarkdownV2 (``parse_mode=MarkdownV2``)."""
        return self._fit(investment, change, _TelegramStyle, TELEGRAM_LIMIT)

    def discord(self, investment: Investment, change: StatusChange | None = None) -> str:
        """Wiadomość w Markdown Discorda (pole ``content`` webhooka)."""
        return self._fit(investment, change, _DiscordStyle, DISCORD_LIMIT)

    def _fit(self, investment: Investment, change: StatusChange | None, style: Any, limit: int) -> str:
        text = ""
        for description_limit in (self.description_limit, 200, 80):
            text = self._render(investment, change, style, description_limit)
            if len(text) <= limit:
                return text
        return text[:limit]

    def _render(self, inv: Investment, change: StatusChange | None, style: Any, description_limit: int) -> str:
        if change is not None and change.stary_status:
            icon, title, detail = "🔄", "ZMIANA STATUSU", (
                f"{_status_word(change.stary_status)} → {_status_word(change.nowy_status)}"
            )
        else:
            icon, title = "🏗️", "NOWY LEAD"
            detail = "pozwolenie na budowę" if inv.zrodlo == "pozwolenia" else "zgłoszenie budowy"

        lines = [
            f"{icon} {style.bold(title)} · {style.text(detail)}",
            style.bold(_truncate(inv.nazwa_zamierzenia or "(brak opisu zamierzenia)", description_limit)),
            "",
        ]
        for line in _details(inv):
            value = style.code(line.value) if line.code else style.text(line.value)
            lines.append(f"{line.icon} {style.text(line.label)}: {value}")

        links = [(label, url) for label, url in (("Google Maps", inv.google_maps_url),
                                                 ("Geoportal", inv.geoportal_url)) if url]
        if links:
            lines += ["", " · ".join(style.link(label, url) for label, url in links)]
        return "\n".join(lines)


def _details(inv: Investment) -> list[_Line]:
    lines = [_Line("📌", "Status", _status_label(inv.status))]

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
        lines.append(_Line("📦", "Kubatura", format(inv.kubatura, ",.0f").replace(",", " ") + " m³"))

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
        lines.append(_Line("🧩", "Działka", inv.teryt_dzialki + (f" (+{extra})" if extra > 0 else ""), code=True))
    lines.append(_Line("🔖", "Sprawa", inv.id_sprawy, code=True))
    return lines


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

class Notifier(Protocol):
    """Kanał powiadomień."""

    channel: str

    def send(self, text: str) -> None: ...


class TelegramNotifier:
    """Wysyła wiadomości przez Bot API (``sendMessage`` z ``parse_mode=MarkdownV2``)."""

    channel = "telegram"

    def __init__(self, http: ResilientHttpClient, bot_token: str, chat_id: str) -> None:
        if not bot_token or not chat_id:
            raise NotificationError(
                "Brak konfiguracji Telegrama – ustaw TELEGRAM_BOT_TOKEN i TELEGRAM_CHAT_ID (sekcja telegram)"
            )
        self.http = http
        self._url = TELEGRAM_API_URL.format(token=bot_token)
        self.chat_id = chat_id

    def send(self, text: str) -> None:
        """Wysyła wiadomość; rzuca :class:`NotificationError`, gdy Telegram ją odrzuci."""
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "link_preview_options": {"is_disabled": True},
        }
        try:
            response = self.http.post(self._url, json=payload)
        except HttpError as exc:
            raise NotificationError(f"Telegram: {exc}") from exc
        body = _json(response)
        if response.status_code != 200 or not body.get("ok"):
            raise NotificationError(
                f"Telegram odrzucił wiadomość (HTTP {response.status_code}): {body.get('description', '')}"
            )


class DiscordNotifier:
    """Wysyła wiadomości na webhook Discorda (bez wzmianek @everyone i bez podglądów linków)."""

    channel = "discord"

    def __init__(self, http: ResilientHttpClient, webhook_url: str) -> None:
        if not webhook_url:
            raise NotificationError("Brak konfiguracji Discorda – ustaw DISCORD_WEBHOOK_URL (sekcja discord)")
        self.http = http
        self._url = webhook_url

    def send(self, text: str) -> None:
        """Wysyła wiadomość; rzuca :class:`NotificationError`, gdy Discord ją odrzuci."""
        payload = {"content": text, "allowed_mentions": {"parse": []}, "flags": DISCORD_SUPPRESS_EMBEDS}
        try:
            response = self.http.post(self._url, json=payload)
        except HttpError as exc:
            raise NotificationError(f"Discord: {exc}") from exc
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
        """Aktualizuje istniejące wiersze (po ``id_sprawy``) i dopisuje nowe – łącznie 3 wywołania API."""
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
