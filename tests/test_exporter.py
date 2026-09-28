import re

import pytest

from gunb_tool.exporter import (
    GoogleSheetsExporter,
    MessageFormatter,
    NotificationError,
    SHEET_COLUMNS,
    DiscordNotifier,
    TelegramNotifier,
    escape_markdown_v2,
)
from gunb_tool.models import Investment
from gunb_tool.storage import StatusChange
from tests.fakes import FakeResponse, make_client

MAPS = "https://www.google.com/maps/search/?api=1&query=50.470000,17.330000"
GEOPORTAL = "https://mapy.geoportal.gov.pl/imap/Imgp_2.html?identifyParcel=160705_4.0005.13"


def lead(**overrides) -> Investment:
    base = dict(
        id_sprawy="ST-OP-NY/WNIOSEK/12/2026",
        zrodlo="pozwolenia",
        status="decyzja",
        data_aktualizacji="2026-09-25",
        data_wplywu="2026-08-01",
        data_decyzji="2026-09-25",
        numer_decyzji="10/2026",
        organ="Starosta Powiatu Nyskiego",
        kategoria="mieszkaniowa-jednorodzinna",
        kategoria_obiektu="I",
        nazwa_zamierzenia="Budowa budynku mieszkalnego jednorodzinnego (parterowy) z garażem",
        adres_opisowy="ul. Testowa 5, Nysa",
        miejscowosc="Nysa",
        powiat="powiat nyski",
        gmina="Nysa",
        teryt_dzialki="160705_4.0005.13",
        dzialki=["160705_4.0005.13", "160705_4.0005.14"],
        lat=50.47,
        lon=17.33,
        precyzja_geo="dzialka",
        google_maps_url=MAPS,
        geoportal_url=GEOPORTAL,
        inwestor="Firma Testowa Sp. z o.o.",
        projektant="Jan Testowy",
        projektant_uprawnienia="OPL/0001/PWOA/20",
        kubatura=1234.5,
        is_residential=True,
    )
    base.update(overrides)
    return Investment(**base)


NEW = StatusChange("ST-OP-NY/WNIOSEK/12/2026", None, "decyzja", "2026-09-28T08:00:00+00:00")
CHANGED = StatusChange("ST-OP-NY/WNIOSEK/12/2026", "wniosek", "decyzja", "2026-09-28T08:00:00+00:00")


# --- Escaping -----------------------------------------------------------------

def test_escape_markdown_v2_escapes_every_reserved_character():
    raw = "_*[]()~`>#+-=|{}.!\\"
    assert escape_markdown_v2(raw) == "".join("\\" + ch for ch in raw)
    assert escape_markdown_v2("Łódź 12") == "Łódź 12"


# --- Telegram -------------------------------------------------------------------

def test_telegram_message_for_new_lead_contains_escaped_details_and_links():
    text = MessageFormatter().telegram(lead(), NEW)

    assert text.startswith("🏗️ *NOWY LEAD*")
    assert "*Budowa budynku mieszkalnego jednorodzinnego \\(parterowy\\) z garażem*" in text
    assert "ul\\. Testowa 5, Nysa" in text
    assert "Firma Testowa Sp\\. z o\\.o\\." in text
    assert "Jan Testowy \\(upr\\. OPL/0001/PWOA/20\\)" in text
    assert "1 234 m³" in text
    assert "`ST-OP-NY/WNIOSEK/12/2026`" in text
    assert f"[Google Maps]({MAPS})" in text
    assert f"[Geoportal]({GEOPORTAL})" in text
    # Poza składnią encji żaden znak zastrzeżony nie może zostać bez escapowania.
    plain = re.sub(r"`[^`]*`|\]\([^)]*\)", "", text)
    assert not re.search(r"(?<!\\)[.!()\-]", plain.replace("[", "").replace("]", ""))


def test_telegram_message_for_status_change_has_transition_header():
    text = MessageFormatter().telegram(lead(), CHANGED)
    assert text.startswith("🔄 *ZMIANA STATUSU*")
    assert "wniosek → decyzja" in text


def test_design_studio_is_not_repeated_when_it_is_the_designer():
    studio = "Pracownia Testowa s.c."
    text = MessageFormatter().discord(lead(projektant=studio, pracownia=studio, projektant_uprawnienia=None), NEW)
    assert text.count("Pracownia Testowa") == 1


def test_long_description_is_truncated():
    text = MessageFormatter(description_limit=40).telegram(lead(nazwa_zamierzenia="Budowa " + "x" * 500), NEW)
    assert "…" in text
    assert "x" * 100 not in text


def test_missing_investor_location_and_approximate_position_are_described():
    text = MessageFormatter().telegram(
        lead(inwestor=None, precyzja_geo="obreb", geoportal_url=None), NEW
    )
    assert "niejawny" in text
    assert "środek obrębu" in text
    assert "Geoportal" not in text
    no_location = MessageFormatter().telegram(lead(lat=None, lon=None, google_maps_url=None, geoportal_url=None), NEW)
    assert "Google Maps" not in no_location


# --- Discord ---------------------------------------------------------------------

def test_discord_message_uses_discord_markdown():
    text = MessageFormatter().discord(lead(nazwa_zamierzenia="Hala *magazynowa* _A_"), NEW)
    assert text.startswith("🏗️ **NOWY LEAD**")
    assert "**Hala \\*magazynowa\\* \\_A\\_**" in text
    assert f"[Google Maps]({MAPS})" in text
    assert "ul. Testowa 5" in text  # kropki nie są escapowane
    assert len(text) <= 2000


# --- Wysyłka -----------------------------------------------------------------------

def test_telegram_notifier_posts_markdown_v2_message():
    http, session, _ = make_client([FakeResponse(200, json_data={"ok": True, "result": {}})])
    TelegramNotifier(http, "123:ABC", "-100200").send("*hej*")

    call = session.calls[0]
    assert call.url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert call.json["chat_id"] == "-100200"
    assert call.json["parse_mode"] == "MarkdownV2"
    assert call.json["text"] == "*hej*"


def test_telegram_notifier_raises_without_leaking_token():
    http, _, _ = make_client([FakeResponse(400, json_data={"ok": False, "description": "Bad Request: can't parse"})])
    with pytest.raises(NotificationError) as excinfo:
        TelegramNotifier(http, "123:ABC", "-100200").send("*hej")
    assert "can't parse" in str(excinfo.value)
    assert "ABC" not in str(excinfo.value)


def test_notifiers_require_credentials():
    http, _, _ = make_client([])
    with pytest.raises(NotificationError, match="TELEGRAM_BOT_TOKEN"):
        TelegramNotifier(http, "", "-100")
    with pytest.raises(NotificationError, match="DISCORD_WEBHOOK_URL"):
        DiscordNotifier(http, "")


def test_discord_notifier_disables_mentions_and_embeds():
    http, session, _ = make_client([FakeResponse(204)])
    DiscordNotifier(http, "https://discord.com/api/webhooks/1/secret").send("**hej** @everyone")
    payload = session.calls[0].json
    assert payload["content"] == "**hej** @everyone"
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["flags"] == 4


def test_discord_notifier_raises_on_error_status():
    http, _, _ = make_client([FakeResponse(400, json_data={"message": "Invalid Form Body"})])
    with pytest.raises(NotificationError, match="Invalid Form Body"):
        DiscordNotifier(http, "https://discord.com/api/webhooks/1/secret").send("x")


# --- Google Sheets -------------------------------------------------------------------

class FakeWorksheet:
    """Atrapa ``gspread.Worksheet`` przechowująca komórki w pamięci."""

    def __init__(self, values=None):
        self.values = [list(row) for row in (values or [])]
        self.batch_calls = []
        self.append_calls = []

    def get_all_values(self):
        return [list(row) for row in self.values]

    def batch_update(self, data, raw=True):
        self.batch_calls.append((data, raw))
        for item in data:
            start = re.match(r"A(\d+):", item["range"])
            row_number = int(start.group(1))
            while len(self.values) < row_number:
                self.values.append([])
            self.values[row_number - 1] = list(item["values"][0])

    def append_rows(self, rows, value_input_option="RAW"):
        self.append_calls.append((rows, value_input_option))
        self.values.extend(list(r) for r in rows)


HEADER = [label for _, label in SHEET_COLUMNS]


def test_sheets_export_writes_header_and_appends_rows_to_empty_sheet():
    sheet = FakeWorksheet()
    result = GoogleSheetsExporter(lambda: sheet).export([lead()])

    assert (result.updated, result.appended) == (0, 1)
    assert sheet.values[0] == HEADER
    row = dict(zip(HEADER, sheet.values[1]))
    assert row["ID sprawy"] == "ST-OP-NY/WNIOSEK/12/2026"
    assert row["Mieszkaniowa"] == "tak"
    assert row["Inwestor"] == "Firma Testowa Sp. z o.o."
    assert row["Lat"] == 50.47
    assert sheet.append_calls[0][1] == "RAW"


def test_sheets_export_updates_existing_row_and_appends_new_ones():
    existing = lead(projektant="Stary Projektant")
    sheet = FakeWorksheet()
    GoogleSheetsExporter(lambda: sheet).export([existing])

    result = GoogleSheetsExporter(lambda: sheet).export([lead(projektant="Nowy Projektant"),
                                                         lead(id_sprawy="INNA/1/2026")])

    assert (result.updated, result.appended) == (1, 1)
    assert len(sheet.values) == 3
    rows = {r[0]: dict(zip(HEADER, r)) for r in sheet.values[1:]}
    assert rows["ST-OP-NY/WNIOSEK/12/2026"]["Projektant"] == "Nowy Projektant"
    assert "INNA/1/2026" in rows


def test_sheets_export_skips_api_calls_when_nothing_to_sync():
    calls = []
    result = GoogleSheetsExporter(lambda: calls.append("open")).export([])
    assert (result.updated, result.appended) == (0, 0)
    assert calls == []
