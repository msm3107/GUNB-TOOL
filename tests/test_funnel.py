"""P1: pomiar od wejścia do płatności – aktywacja z surowych zdarzeń, kohorty z zakończoną obserwacją
w mianowniku, osoby obok zdarzeń, przyznanie dostępu osobno od płatności; diagnostyka danych i retencja."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gunb_tool.bot_store import BotStore, BotUser, Event
from gunb_tool.config import OfferConfig
from gunb_tool.funnel import ACTIVATION, ActivationRule, activation_time, trial_cohort
from tests.bot_helpers import ADMIN, MIETEK, OBCY, click, configured, lead, make_bot, message

START = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
OFFER = OfferConfig(price=Decimal("99"), tax="brutto", payment="przelew", seller_name="Jan", seller_contact="@jan")


def ev(kind, hours, inv=None, detail=None, chat_id=1):
    return Event(chat_id, kind, inv, (START + timedelta(hours=hours)).isoformat(timespec="seconds"), detail)


# --- Aktywacja ----------------------------------------------------------------------------------------------

def test_activation_needs_three_different_openings_and_a_save_or_positive_rating_within_48h():
    opened = [ev("szczegoly", 1, "A"), ev("szczegoly", 2, "B"), ev("szczegoly", 3, "C")]
    assert activation_time(opened + [ev("zapis", 5, "A")], START) == START + timedelta(hours=5)
    assert activation_time(opened + [ev("wynik", 4, "B", "rozmowa")], START) == START + timedelta(hours=4)
    assert activation_time(opened + [ev("przydatne", 6, "C")], START) is not None
    assert activation_time(opened, START) is None  # same otwarcia to za mało
    assert activation_time(opened + [ev("wynik", 4, "B", "niepasujaca")], START) is None
    assert activation_time(opened + [ev("zapis", 49, "A")], START) is None  # po 48 h
    same = [ev("szczegoly", h, "A") for h in (1, 2, 3)] + [ev("zapis", 4, "A")]
    assert activation_time(same, START) is None  # trzy otwarcia tej samej inwestycji to jedna


def test_activation_rule_is_versioned_and_can_change_without_rewriting_history():
    events = [ev("szczegoly", 1, "A"), ev("szczegoly", 2, "B"), ev("zapis", 3, "A")]
    assert activation_time(events, START) is None
    lenient = ActivationRule(version="v2", opened=2, within=timedelta(hours=72))
    assert activation_time(events, START, lenient) is not None and "v1" in ACTIVATION.describe()


# --- Kohorty ------------------------------------------------------------------------------------------------

def person(chat_id, started_days_ago, now):
    start = (now - timedelta(days=started_days_ago)).isoformat(timespec="seconds")
    return BotUser(chat_id=chat_id, imie=None, username=None, status="aktywny", tryb="rano", tylko_hot=False,
                   test_start=start, rodzaj_dostepu="test")


def test_only_cohorts_with_finished_observation_count_in_conversion():
    now = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    a, b, c = person(1, 20, now), person(2, 20, now), person(3, 3, now)
    start_a = datetime.fromisoformat(a.test_start)
    events = [Event(1, kind, inv, (start_a + timedelta(hours=h)).isoformat(), detail) for kind, inv, h, detail in (
        ("szczegoly", "A", 1, None), ("szczegoly", "B", 2, None), ("szczegoly", "C", 3, None), ("zapis", "A", 4, None),
        ("zamowienie", None, 150, "Z-1"), ("platnosc", None, 170, "Z-1"))]
    cohort = trial_cohort([a, b, c], events, since=now - timedelta(days=30), now=now)
    assert (cohort.started, cohort.observed, cohort.ongoing) == ([1, 2, 3], [1, 2], [3])
    assert (cohort.activated, cohort.ordered, cohort.paid) == ([1], [1], [1])


# --- Raport admina ----------------------------------------------------------------------------------------------

@pytest.fixture
def bot(repo, api, clock):
    return make_bot(repo, api, clock, offer=OFFER)


def trial(bot, api, chat_id, source):
    bot.handle_update(message(chat_id, f"/start {source}"))
    configured(bot, chat_id)
    bot.handle_update(click(ADMIN, f"adm:trial:{chat_id}"))
    bot.handle_update(click(chat_id, "ts"))


def test_admin_report_separates_people_events_delivery_access_and_payment(bot, api, repo, clock):
    for n in range(3):
        repo.upsert(lead(f"R/{n}", data_aktualizacji="2026-09-28"))
    trial(bot, api, MIETEK, "strona")
    trial(bot, api, OBCY, "ulotka")
    bot.deliver_reports("rano")
    nrs = [repo.get(f"R/{n}").nr for n in range(3)]
    for nr in nrs + nrs[:1]:
        bot.handle_update(click(MIETEK, f"o:{nr}"))  # 3 inwestycje, 4 otwarcia, jedna osoba
    bot.handle_update(click(MIETEK, f"s1:{nrs[0]}"))
    bot.handle_update(click(MIETEK, f"w:{nrs[1]}:n"))
    bot.handle_update(click(MIETEK, f"wp:{nrs[1]}:a"))
    bot.handle_update(click(MIETEK, "zm:new"))
    bot.handle_update(click(ADMIN, f"adm:pay:{BotStore(repo).orders()[0].id}"))
    bot.handle_update(message(ADMIN, "/start"))
    clock.advance(days=15)

    bot.handle_update(message(ADMIN, "/raport 30"))
    report = api.last_to(ADMIN)["text"]

    assert "📈 <b>Pilotaż – ostatnie 30 dni</b>" in report
    assert "nowe 3" in report and "strona 1" in report and "ulotka 1" in report
    assert "Otwarte szczegóły: 3 inwestycje · 1 osoba · 4 otwarcia" in report
    assert "Zapisane: 1 inwestycja · 1 osoba" in report
    assert "niepasujące 1 (obszar 1)" in report
    assert "Wysłane w raportach i alertach: 3 inwestycje · 2 osoby" in report
    assert "🛒 Zamówienia: 1 osoba · 💳 płatności potwierdzone: 1 osoba" in report
    assert "obserwacja zakończona: 2 → aktywacja 1/2 · zamówienie 1/2 · płatność 1/2" in report
    assert "trwające: 0" in report
    assert "aktywacja (v1)" in report and "wysłanie to nie przeczytanie" in report
    assert "mapy" in report  # kliknięć w mapy nie mierzymy i nie raportujemy


def test_short_report_does_not_count_ongoing_trials_as_failures(bot, api, repo):
    trial(bot, api, MIETEK, "strona")
    bot.handle_update(message(ADMIN, "/raport 7"))
    report = api.last_to(ADMIN)["text"]
    assert "obserwacja zakończona: 0" in report and "trwające: 1" in report


# --- Diagnostyka danych i retencja --------------------------------------------------------------------------

def test_data_diagnostics_flag_inconsistent_names_codes_and_missing_offer(repo, api, clock):
    bot = make_bot(repo, api, clock)  # oferta niepełna
    repo.upsert(lead("D/1", powiat="olsztyński", powiat_teryt="1465", gmina_teryt="1465011", gmina="Dywity",
                     data_decyzji="2026-09-20", precyzja_geo="dzialka", lat=53.8, lon=20.4))
    repo.upsert(lead("D/2", powiat="powiat olsztyński", powiat_teryt="1465", gmina_teryt="1465011",
                     gmina="Dywity (gmina)", data_decyzji="2026-09-21", precyzja_geo="obreb", lat=53.8, lon=20.4))
    repo.upsert(lead("D/3", powiat_teryt="0201", gmina=None, miejscowosc=None, adres_opisowy=None))  # spoza
    bot.handle_update(message(ADMIN, "/dane"))
    text = api.last_to(ADMIN)["text"]
    assert "🧪 <b>Dane w bocie</b>" in text
    assert "Kod 1465 ma różne nazwy w danych: „olsztyński”, „powiat olsztyński”" in text
    assert "Gmina 1465011 ma różne nazwy: „Dywity”, „Dywity (gmina)”" in text
    assert "Spoza ustawionych powiatów: 1" in text and "bez gminy: 1" in text
    assert "dokładna 1 · przybliżona 1 · brak 1" in text
    assert "Oferta niepełna – brakuje: cena" in text
    assert "--historical" in text  # brak starszych decyzji do okien etapów


def test_old_events_are_removed_by_the_nightly_cleanup(bot, api, repo, clock):
    store = BotStore(repo)
    store.record_event(MIETEK, "demo")
    clock.advance(days=400)
    store.record_event(MIETEK, "oferta")
    clock.utc = clock.utc.replace(hour=3, minute=0)  # 04:00 czasu polskiego (zima) – po nocnych porządkach
    bot.run_due_jobs()
    assert [e.rodzaj for e in store.events()] == ["oferta"]
