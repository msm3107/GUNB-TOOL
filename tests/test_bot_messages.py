"""P1.2: uczciwe komunikaty – szacunki nazwane szacunkami, świeżość danych, „inwestycja” zamiast „lead”."""

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from gunb_tool import bot_ui as ui
from gunb_tool.bot_store import BotStore, BotUser, UserFilters
from gunb_tool.config import BotConfig
from gunb_tool.http_client import HttpError
from gunb_tool.stages import LONGEST_WINDOW_DAYS, get_trade
from tests.bot_helpers import ADMIN, MIETEK, activate, click, lead, make_bot, message

ROOT = Path(__file__).resolve().parent.parent
BASE = (53.7784, 20.4801)
NEAR = dict(lat=53.8285, lon=20.4867, miejscowosc="Dywity")  # ok. 6 km od bazy


def decided(months: float) -> dict:
    day = (date(2026, 9, 29) - timedelta(days=round(months * 30.44))).isoformat()
    return dict(data_decyzji=day, data_aktualizacji=day)


@pytest.fixture
def bot(repo, api, clock):
    bot = make_bot(repo, api, clock)
    activate(bot, api)
    return bot


def pin(chat_id, lat, lon):
    update = message(chat_id, "")
    update["message"]["location"] = {"latitude": lat, "longitude": lon}
    return update


# --- Szacunki nazwane szacunkami ------------------------------------------------------------------

def test_stage_reminder_calls_the_stage_an_estimate(bot, api, repo):
    repo.upsert(lead("DACH/1", nazwa_zamierzenia="Dom na etapie dachu", **decided(4.5)))
    BotStore(repo).set_trade(MIETEK, "dach")

    bot.deliver_stage_reminders()

    text = api.last_to(MIETEK)["text"]
    assert "Warto sprawdzić tę inwestycję" in text
    assert "Orientacyjne okno dla Twojej branży" in text
    assert "Szacunek na podstawie daty decyzji; rzeczywisty etap wymaga sprawdzenia" in text


def test_distance_is_a_straight_line_not_a_drive(bot, api, repo):
    repo.upsert(lead("BLISKO/1", nazwa_zamierzenia="Dom w Dywitach", **NEAR))
    bot.handle_update(pin(MIETEK, *BASE))
    bot.handle_update(message(MIETEK, "📊 Inwestycje"))
    report = api.last_to(MIETEK)["text"]
    bot.handle_update(click(MIETEK, f"o:{repo.get('BLISKO/1').nr}"))
    card = api.last_to(MIETEK)["text"]

    assert "🚗" not in report + card
    assert "📏 6 km" in report
    assert "6 km w linii prostej od Twojej bazy" in card


def test_card_keeps_the_approximate_location_note(bot, api, repo):
    repo.upsert(lead("OBR/1", precyzja_geo="obreb", gmina="Dywity", powiat="powiat olsztyński"))
    bot.handle_update(click(MIETEK, f"o:{repo.get('OBR/1').nr}"))
    assert "lokalizacja przybliżona – środek obrębu" in api.last_to(MIETEK)["text"]


def test_scale_is_an_estimate_not_a_chance_of_winning(bot, api, repo):
    repo.upsert(lead("HOT/1", priorytet="hot", punkty=10, kubatura=26000.0))
    bot.handle_update(message(MIETEK, "📊 Inwestycje"))
    report = api.last_to(MIETEK)["text"]
    bot.handle_update(click(MIETEK, f"o:{repo.get('HOT/1').nr}"))
    card = api.last_to(MIETEK)["text"]
    bot.handle_update(message(MIETEK, "❓ Pomoc"))
    help_text = api.last_to(MIETEK)["text"]

    assert "🏗️ Skala: duża (26 000 m³)" in card and "📐 <b>Szacunki</b> (orientacyjne)" in card
    assert "Dużej skali (szacunek): 1." in report and "HOT" not in report + card
    assert "szacunek skali" in help_text and "nie mówi, czy zdobędziesz zlecenie" in help_text


def test_screens_say_investment_and_never_promise_the_investors_phone(bot, api, repo):
    repo.upsert(lead("DACH/1", nazwa_zamierzenia="Dom na etapie dachu", **decided(4.5)))
    repo.upsert(lead("N/1"))
    BotStore(repo).set_trade(MIETEK, "dach")
    settings = BotConfig()
    inv = repo.get("N/1")
    trade = get_trade("dach")
    texts = [
        ui.welcome_text("Mietek"), ui.help_text(settings), ui.mode_screen(BotStore(repo).get_user(MIETEK), settings)[0],
        ui.hidden_card(inv)[0], ui.watch_screen([])[0], ui.saved_list([], 0, 0)[0], ui.hot_only_text(True),
        ui.hot_only_text(False), ui.trade_picker("dach")[0], ui.stage_none_text(trade), ui.setup_trade_step()[0],
        ui.report("29.09", total_new=1, leads=[inv], matching=1, hot=0, watched=0)[0],
        bot.formatter.telegram(inv).text,
    ]
    bot.deliver_stage_reminders()
    texts.append(api.last_to(MIETEK)["text"])

    for text in texts:
        lowered = text.lower()
        assert "lead" not in lowered, text
        assert not any(word in lowered for word in ("telefon", "zadzwo", "dzwoni")), text


# --- Świeżość danych --------------------------------------------------------------------------------

def run_import(bot, clock, fetcher):
    bot.settings = BotConfig(**{**bot.settings.__dict__, "fetch_times": ("06:30",)})
    bot.fetcher = fetcher
    clock.utc = datetime(2026, 9, 29, 4, 31, tzinfo=timezone.utc)  # 06:31
    bot.run_due_jobs()


def test_report_says_when_the_registry_was_last_checked(bot, api, repo, clock):
    repo.upsert(lead("A/1"))
    run_import(bot, clock, lambda: "nowe 1")
    bot.handle_update(message(MIETEK, "📊 Inwestycje"))
    assert "Rejestr GUNB sprawdzony: 29.09.2026, 06:31" in api.last_to(MIETEK)["text"]


def test_nothing_new_while_the_registry_is_being_checked(bot, api, repo):
    repo.acquire_lease("import", "watek-zadan", timedelta(minutes=30))  # import trwa (ma blokadę)
    BotStore(repo).job_started("import")
    bot.handle_update(message(MIETEK, "📊 Inwestycje"))
    text = api.last_to(MIETEK)["text"]
    assert "Nic nowego" in text and "Właśnie sprawdzam rejestr" in text


def test_nothing_new_after_a_failed_check_says_the_data_may_be_stale(bot, api, repo, clock):
    def failing():
        raise HttpError("HTTP 503", status_code=503)

    run_import(bot, clock, failing)
    bot.handle_update(message(MIETEK, "📊 Inwestycje"))
    text = api.last_to(MIETEK)["text"]
    assert "Ostatnie sprawdzenie rejestru się nie udało" in text and "ponowię o 07:31" in text


def test_no_empty_scheduled_report_even_when_the_check_failed(bot, api, repo, clock):
    def failing():
        raise HttpError("HTTP 503", status_code=503)

    run_import(bot, clock, failing)
    api.sent.clear()
    clock.utc = datetime(2026, 9, 29, 5, 1, tzinfo=timezone.utc)  # 07:01 – raport, a nic nowego
    bot.run_due_jobs()
    assert api.to(MIETEK) == []


# --- Historia zgodna z oknami etapów -------------------------------------------------------------------

def test_history_import_covers_the_longest_stage_window_by_default():
    import main

    parser = main.build_parser()
    args = parser.parse_args(["--fetch", "--historical"])
    since = main.history_start(args, today=date(2026, 9, 29))
    assert since == date(2026, 9, 29) - timedelta(days=LONGEST_WINDOW_DAYS)
    assert main.history_start(parser.parse_args(["--fetch", "--historical", "--since", "2026-01-01"]),
                              today=date(2026, 9, 29)) == date(2026, 1, 1)


def test_installer_uses_the_same_history_window():
    script = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "18 months" not in script and "--historical" in script


def test_filters_summary_escapes_every_part_exactly_once():
    person = BotUser(chat_id=1, imie=None, username=None, status="aktywny", tryb="rano", tylko_hot=False,
                     filtry=UserFilters(miejsca=("Kowale & Syn <x>",), inwestor="Dom & Ogród"))
    text = ui.filters_summary(person, {})
    assert "Kowale &amp; Syn &lt;x&gt;" in text and "Dom &amp; Ogród" in text
    assert "&amp;amp;" not in text and " & " not in text


def test_filters_are_never_widened_behind_the_users_back(bot, api, repo):
    BotStore(repo).set_filters(MIETEK, UserFilters(powiaty=("3021",)))
    bot.handle_update(message(MIETEK, "📊 Inwestycje"))
    assert BotStore(repo).get_user(MIETEK).filtry == UserFilters(powiaty=("3021",))
    assert ADMIN not in [m["chat_id"] for m in api.sent]


def test_import_left_running_by_a_dead_process_is_not_called_running(bot, api, repo):
    BotStore(repo).job_started("import")  # „trwa”, ale nikt nie trzyma blokady – proces zginął
    bot.handle_update(message(MIETEK, "📊 Inwestycje"))
    text = api.last_to(MIETEK)["text"]
    assert "Właśnie sprawdzam" not in text and "nie zakończyło się" in text


def test_new_sales_screens_promise_nothing_the_product_does_not_do(bot, api, repo):
    """Opis, przykład, oferta, podsumowanie, podpowiedzi i wiadomości o dostępie: bez „lead”, bez telefonów do
    inwestorów, bez HOT, bez gwarancji (poza zaprzeczeniem) i bez sugerowania płatności tam, gdzie jej nie było."""
    from decimal import Decimal

    from gunb_tool.config import OfferConfig
    from gunb_tool.demo import demo_leads

    offer = OfferConfig(area="Olsztyn i powiat olsztyński", price=Decimal("99"), tax="brutto", payment="przelew",
                        seller_name="Jan", seller_contact="@jan", response_time="w dni robocze 16:00–20:00")
    leads = demo_leads(date(2026, 9, 29))
    user = BotStore(repo).get_user(MIETEK)
    summary = {"dostarczone": 4, "otwarte": 2, "zapisane": 1, "notatki": 0, "przypomnienia": 0, "wyniki": 1}
    texts = [
        ui.intro_text("Mietek", offer.area, offer), ui.intro_text("Mietek", offer.area, OfferConfig()),
        ui.gate_short_text(requested_on=None), ui.about_text(offer.area), ui.trial_requested_text(offer),
        ui.offer_text(offer, offer.area), ui.offer_text(OfferConfig(), offer.area),
        ui.demo_screen(leads, ui.lead_card(leads[0]))[0], ui.trial_nudge(summary, "06.10.2026, 07:00")[0],
        ui.access_reminder_text("06.10.2026", trial=True, offer=offer, summary=summary),
        ui.access_ended_text("06.10.2026", trial=True, offer=offer), ui.activated_text("29.10.2026", "Mietek", days=30),
        ui.setup_summary(user, "Dywity", BotConfig(), can_start_trial=True, area=offer.area, data_range=None,
                         freshness="", recent=0, in_window=0)[0],
        ui.first_value(leads, recent=3, in_window_ids=set(), trade=None, days=30, tip=True)[0],
        ui.TIP_OPEN, ui.TIP_SAVE, ui.TIP_NOTE, ui.rejected_text(),
    ]
    for text in texts:
        lowered = text.lower()
        assert "lead" not in lowered and "hot" not in lowered.replace("hotel", ""), text
        assert not any(word in lowered for word in ("telefon", "zadzwo", "dzwoni")), text
        assert all("nie" in words for words in re.findall(r"(\S+) (\S+) gwaranc", lowered)), text
    assert "płatn" not in ui.activated_text("29.10.2026", "Mietek", days=30).lower()


def test_long_admin_text_is_split_between_lines_into_messages_telegram_accepts():
    from tests.bot_helpers import telegram_rejects

    lines = ["👥 <b>Użytkownicy</b>"] + [f"✅ Firma {n} &amp; Syn ({n})" for n in range(400)] + [""]
    parts = ui.split_lines("\n".join(lines), limit=500)
    assert len(parts) > 1 and all(telegram_rejects(part) is None and len(part) <= 500 for part in parts)
    assert "\n".join(parts).split("\n") == lines[:-1]  # nic nie ginie, kolejność zostaje
    assert ui.split_lines("krótko") == ["krótko"]

    single = "<b>" + "A &amp; B " * 100 + "</b>"  # jedna linia dłuższa niż limit – bez znaczników, przed encją
    (cut,) = ui.split_lines(single, limit=101)
    assert telegram_rejects(cut) is None and len(cut) <= 101 and cut.endswith("…")
