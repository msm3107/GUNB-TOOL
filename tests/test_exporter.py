import re

import pytest

from gunb_tool.exporter import (
    GoogleSheetsExporter,
    MessageFormatter,
    NotificationError,
    OutgoingMessage,
    RateLimiter,
    SHEET_COLUMNS,
    DiscordNotifier,
    TelegramNotifier,
    plural_leads,
)
from gunb_tool.models import Investment
from gunb_tool.storage import StatusChange
from tests.fakes import FakeResponse, FakeTime, make_client

MAPS = "https://www.google.com/maps?q=50.470000,17.330000"
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


# --- Telegram: HTML + przyciski ------------------------------------------------------------

def test_telegram_message_is_html_with_details():
    message = MessageFormatter().telegram(lead(), NEW)

    assert message.text.startswith("🏗️ <b>NOWY LEAD</b> · pozwolenie na budowę")
    assert "<b>Budowa budynku mieszkalnego jednorodzinnego (parterowy) z garażem</b>" in message.text
    assert "ul. Testowa 5, Nysa" in message.text  # w HTML kropki i nawiasy nie wymagają escapowania
    assert "Jan Testowy (upr. OPL/0001/PWOA/20)" in message.text
    assert "1 234 m³" in message.text
    assert "🔖 Sprawa: <code>ST-OP-NY/WNIOSEK/12/2026</code>" in message.text
    assert "\\" not in message.text  # żadnych śladów escapowania MarkdownV2
    assert message.lead_ids == ("ST-OP-NY/WNIOSEK/12/2026",)


def test_telegram_message_escapes_html_special_characters():
    text = MessageFormatter().telegram(lead(inwestor="Kowalski & Syn <Sp. j.>", nazwa_zamierzenia="Hala > 2 000 m²"), NEW).text
    assert "Kowalski &amp; Syn &lt;Sp. j.&gt;" in text
    assert "<b>Hala &gt; 2 000 m²</b>" in text
    assert "<Sp." not in text


def test_telegram_links_are_inline_keyboard_buttons_not_text():
    message = MessageFormatter().telegram(lead(), NEW)
    assert message.buttons == (("📍 Otwórz w Google Maps", MAPS), ("🏛️ Geoportal", GEOPORTAL))
    assert MAPS not in message.text


def test_telegram_buttons_follow_available_links():
    assert MessageFormatter().telegram(lead(geoportal_url=None), NEW).buttons == (("📍 Otwórz w Google Maps", MAPS),)
    assert MessageFormatter().telegram(lead(google_maps_url=None, geoportal_url=None), NEW).buttons == ()


def test_telegram_status_change_header():
    text = MessageFormatter().telegram(lead(), CHANGED).text
    assert text.startswith("🔄 <b>ZMIANA STATUSU</b> · wniosek → decyzja")


def test_long_description_is_truncated():
    text = MessageFormatter(description_limit=40).telegram(lead(nazwa_zamierzenia="Budowa " + "x" * 500), NEW).text
    assert "…" in text
    assert "x" * 100 not in text


def test_missing_investor_and_approximate_position_are_described():
    text = MessageFormatter().telegram(lead(inwestor=None, precyzja_geo="obreb", geoportal_url=None), NEW).text
    assert "niejawny" in text
    assert "środek obrębu" in text


def test_parcel_count_is_shown_outside_code_span():
    assert "🧩 Działka: <code>160705_4.0005.13</code> (+1)" in MessageFormatter().telegram(lead(), NEW).text


def test_segment_label_is_shown():
    formatter = MessageFormatter(segment_labels={"domki": "Domki jednorodzinne"})
    assert "🎯 Segment: Domki jednorodzinne" in formatter.telegram(lead(segment="domki"), NEW).text
    assert "Segment" not in formatter.telegram(lead(segment=None), NEW).text


# --- Discord ---------------------------------------------------------------------------------

def test_discord_message_uses_discord_markdown_with_links_in_text():
    message = MessageFormatter().discord(lead(nazwa_zamierzenia="Hala *magazynowa* _A_"), NEW)
    assert message.text.startswith("🏗️ **NOWY LEAD**")
    assert "**Hala \\*magazynowa\\* \\_A\\_**" in message.text
    assert f"[Google Maps]({MAPS})" in message.text
    assert message.buttons == ()
    assert len(message.text) <= 2000


def test_design_studio_is_not_repeated_when_it_is_the_designer():
    studio = "Pracownia Testowa s.c."
    text = MessageFormatter().discord(lead(projektant=studio, pracownia=studio, projektant_uprawnienia=None), NEW).text
    assert text.count("Pracownia Testowa") == 1


# --- Raport zbiorczy ----------------------------------------------------------------------------

def many_leads(count: int) -> list[Investment]:
    kinds = ["mieszkaniowa-jednorodzinna", "komercyjna", "mieszkaniowa-wielorodzinna"]
    return [
        lead(id_sprawy=f"ST-XX/WNIOSEK/{n}/2026", kategoria=kinds[n % 3], data_aktualizacji=f"2026-09-{1 + n % 28:02d}",
             nazwa_zamierzenia=f"Budowa budynku nr {n} " + "z infrastrukturą towarzyszącą " * 3)
        for n in range(count)
    ]


def test_telegram_digest_covers_each_lead_once_within_message_limit():
    leads = many_leads(60)
    messages = MessageFormatter().telegram_digest(leads, {})

    assert len(messages) > 1
    assert all(len(m.text) <= 4096 for m in messages)
    covered = [lead_id for m in messages for lead_id in m.lead_ids]
    assert sorted(covered) == sorted(l.id_sprawy for l in leads)
    assert messages[0].text.startswith("📊 <b>Raport GUNB</b> · 60 leadów (1/")
    assert "🏠 mieszkaniowa-jednorodzinna: 20" in messages[0].text
    assert messages[1].text.startswith("📊 <b>Raport GUNB</b> · 60 leadów (2/")


def test_digest_entries_escape_html_and_link_each_lead():
    text = MessageFormatter().telegram_digest([lead(nazwa_zamierzenia="Hala <A> & B")], {})[0].text
    assert "<b>Hala &lt;A&gt; &amp; B</b>" in text
    assert f'<a href="{MAPS}">📍 mapa</a>' in text
    assert f'<a href="{GEOPORTAL}">🏛️ działka</a>' in text


def test_digest_marks_status_changes():
    text = MessageFormatter().telegram_digest([lead()], {"ST-OP-NY/WNIOSEK/12/2026": CHANGED})[0].text
    assert "🔄 wniosek → decyzja" in text


def test_discord_digest_respects_2000_character_limit():
    messages = MessageFormatter().discord_digest(many_leads(40), {})
    assert all(len(m.text) <= 2000 for m in messages)
    assert sum(len(m.lead_ids) for m in messages) == 40


@pytest.mark.parametrize("count,word", [(1, "lead"), (3, "leady"), (11, "leadów"), (14, "leadów"), (22, "leady"), (25, "leadów")])
def test_polish_plural_of_leads(count, word):
    assert plural_leads(count) == f"{count} {word}"


# --- Wysyłka ------------------------------------------------------------------------------------

def test_rate_limiter_keeps_at_least_one_second_between_messages():
    clock = FakeTime()
    limiter = RateLimiter(1.0, clock=clock.clock, sleep=clock.sleep)
    limiter.wait()
    clock.now += 0.25
    limiter.wait()
    limiter.wait()
    assert clock.sleeps == [pytest.approx(0.75), pytest.approx(1.0)]


def test_rate_limiter_counts_interval_from_end_of_previous_send():
    # Przebieg na żywo: pierwsze żądanie trwało dłużej (TLS), a kolejne wyszło po 0,7 s od jego końca.
    clock = FakeTime()
    limiter = RateLimiter(1.0, clock=clock.clock, sleep=clock.sleep)
    limiter.wait()
    clock.now += 0.5  # czas trwania wysyłki
    limiter.touch()
    limiter.wait()
    assert clock.sleeps == [pytest.approx(1.0)]


def test_telegram_notifier_sends_html_with_inline_keyboard():
    http, session, _ = make_client([FakeResponse(200, json_data={"ok": True, "result": {}})])
    notifier = TelegramNotifier(http, "123:ABC", "-100200")
    notifier.send(OutgoingMessage("<b>hej</b>", buttons=(("📍 Otwórz w Google Maps", MAPS), ("🏛️ Geoportal", GEOPORTAL))))

    call = session.calls[0]
    assert call.url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert call.json["chat_id"] == "-100200"
    assert call.json["parse_mode"] == "HTML"
    assert call.json["text"] == "<b>hej</b>"
    assert call.json["reply_markup"] == {"inline_keyboard": [
        [{"text": "📍 Otwórz w Google Maps", "url": MAPS}],
        [{"text": "🏛️ Geoportal", "url": GEOPORTAL}],
    ]}


def test_telegram_notifier_routes_segments_to_their_chats():
    http, session, _ = make_client([FakeResponse(200, json_data={"ok": True})])
    notifier = TelegramNotifier(http, "123:ABC", "-100200", segment_chats={"domki": "-100777"})
    assert notifier.destination("domki") == "-100777"
    assert notifier.destination("duze") == "-100200"
    assert notifier.destination(None) == "-100200"
    notifier.send(OutgoingMessage("x"), notifier.destination("domki"))
    assert session.calls[0].json["chat_id"] == "-100777"
    assert "reply_markup" not in session.calls[0].json


def test_telegram_notifier_is_rate_limited_to_one_message_per_second():
    clock = FakeTime()
    http, _, _ = make_client([FakeResponse(200, json_data={"ok": True})] * 3)
    notifier = TelegramNotifier(http, "123:ABC", "-100200", min_interval=0.2, clock=clock.clock, sleep=clock.sleep)
    for _ in range(3):
        notifier.send(OutgoingMessage("x"))
    assert clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]  # min_interval < 1 s jest podnoszone do 1 s


def test_telegram_notifier_raises_without_leaking_token():
    http, _, _ = make_client([FakeResponse(400, json_data={"ok": False, "description": "Bad Request: can't parse"})])
    with pytest.raises(NotificationError) as excinfo:
        TelegramNotifier(http, "123:ABC", "-100200").send(OutgoingMessage("<b>hej"))
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
    DiscordNotifier(http, "https://discord.com/api/webhooks/1/secret").send(OutgoingMessage("**hej** @everyone"))
    payload = session.calls[0].json
    assert payload["content"] == "**hej** @everyone"
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["flags"] == 4


def test_discord_notifier_raises_on_error_status():
    http, _, _ = make_client([FakeResponse(400, json_data={"message": "Invalid Form Body"})])
    with pytest.raises(NotificationError, match="Invalid Form Body"):
        DiscordNotifier(http, "https://discord.com/api/webhooks/1/secret").send(OutgoingMessage("x"))


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
    result = GoogleSheetsExporter(lambda: sheet).export([lead(segment="domki")])

    assert (result.updated, result.appended) == (0, 1)
    assert sheet.values[0] == HEADER
    row = dict(zip(HEADER, sheet.values[1]))
    assert row["ID sprawy"] == "ST-OP-NY/WNIOSEK/12/2026"
    assert row["Segment"] == "domki"
    assert row["Mieszkaniowa"] == "tak"
    assert row["Inwestor"] == "Firma Testowa Sp. z o.o."
    assert row["Lat"] == 50.47
    assert sheet.append_calls[0][1] == "RAW"


def test_sheets_export_updates_existing_row_and_appends_new_ones():
    sheet = FakeWorksheet()
    GoogleSheetsExporter(lambda: sheet).export([lead(projektant="Stary Projektant")])

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
