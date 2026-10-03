"""P0: czytelna karta inwestycji – fakty z rejestru, szacunki i własna praca osobno; szczegóły urzędowe
po rozwinięciu; skala zamiast HOT; kolejność według zastosowania, miejsca i daty; wynik pracy (P1)."""

from datetime import date, timedelta

import pytest

from gunb_tool.bot_store import BotStore, Outcome, UserFilters
from tests.bot_helpers import MIETEK, OBCY, activate, buttons, click, lead, make_bot, message

BASE = (53.7784, 20.4801)  # Olsztyn
DYWITY = dict(adres_opisowy="Dywity, ul. Polna 5", miejscowosc="Dywity", gmina="Dywity", powiat="olsztyński",
              powiat_teryt="2814", gmina_teryt="2814032", lat=53.8285, lon=20.4867, precyzja_geo="dzialka")


def decided(days_ago: int) -> dict:
    day = (date(2026, 9, 29) - timedelta(days=days_ago)).isoformat()
    return dict(data_decyzji=day, data_aktualizacji=day)


@pytest.fixture
def bot(repo, api, clock):
    bot = make_bot(repo, api, clock)
    activate(bot, api)
    return bot


def open_card(bot, api, repo, id_sprawy, chat_id=MIETEK):
    bot.handle_update(click(chat_id, f"o:{repo.get(id_sprawy).nr}"))
    return api.last_to(chat_id)


def labels(markup):
    return [text for text, _ in buttons(markup)]


# --- Pierwszy poziom karty --------------------------------------------------------------------------------

def test_first_level_shows_type_place_date_location_and_why(bot, api, repo):
    store = BotStore(repo)
    store.set_filters(MIETEK, UserFilters(miejsca=("Dywity",), kategorie=("mieszkaniowa-jednorodzinna",)))
    repo.upsert(lead("K/1", nazwa_zamierzenia="Budowa domu jednorodzinnego z garażem", inwestor=None,
                     organ="Starosta Olsztyński", projektant="Jan Projektant", **DYWITY, **decided(8)))

    card = open_card(bot, api, repo, "K/1")
    text = card["text"]

    assert text.startswith("🏠 <b>Dom jednorodzinny</b> · pozwolenie na budowę")
    assert "<b>Budowa domu jednorodzinnego z garażem</b>" in text
    assert "📍 Dywity, ul. Polna 5" in text and "📅 Decyzja: 21.09.2026" in text
    assert "🗺️ Mapa: lokalizacja dokładna – działka" in text
    assert "🔖 Nr sprawy: <code>K/1</code>" in text  # linku do wpisu GUNB nie ma – numer do wyszukiwarki
    assert "🎯 <b>Dlaczego to widzisz:</b> Dywity · domy jednorodzinne" in text
    for official in ("Organ", "Projektant", "Działka", "Inwestor", "📞"):  # szczegóły – po rozwinięciu; pustych pól brak
        assert official not in text
    assert {"📍 Mapa", "⭐ Zapisz", "⏰ Przypomnij", "📝 Notatka", "📋 Wynik", "🔽 Szczegóły", "⋯ Więcej"} \
        <= set(labels(card["markup"]))


def test_official_details_open_and_close_in_the_same_message(bot, api, repo):
    repo.upsert(lead("K/2", organ="Starosta Olsztyński", projektant="Jan Projektant", teryt_dzialki="281403_2.0012.45",
                     **DYWITY, **decided(8)))
    nr = repo.get("K/2").nr
    open_card(bot, api, repo, "K/2")

    bot.handle_update(click(MIETEK, f"d:{nr}"))
    details = api.edits[-1]
    assert "🔖 Sprawa: K/2" in details["text"] and "🏛️ Organ: Starosta Olsztyński" in details["text"]
    assert "Jan Projektant" in details["text"] and "🔼 Zwiń" in labels(details["markup"])

    bot.handle_update(click(MIETEK, f"d:{nr}:0"))
    assert "Sprawa" not in api.edits[-1]["text"] and "🔽 Szczegóły" in labels(api.edits[-1]["markup"])


def test_scale_replaces_hot_and_is_called_an_estimate(bot, api, repo):
    repo.upsert(lead("BIG/1", priorytet="hot", punkty=10, kubatura=26000.0, kategoria="mieszkaniowa-wielorodzinna",
                     **decided(3)))
    bot.handle_update(message(MIETEK, "📊 Inwestycje"))
    report = api.last_to(MIETEK)["text"]
    card = open_card(bot, api, repo, "BIG/1")["text"]

    assert "HOT" not in report + card and "🔥" not in report + card
    assert "🏗️ Skala: duża (26 000 m³)" in card
    assert "📐 <b>Szacunki</b> (orientacyjne)" in card


@pytest.mark.parametrize("investor, expected", [
    (None, None),  # bez nazwy w rejestrze – bez pola (nie „brak”, nie „osoba prywatna”)
    ("Budimex S.A.", "💼 Inwestor: Budimex S.A. (wg rejestru, bez weryfikacji)"),
])
def test_investor_field_never_guesses_who_it_is(bot, api, repo, investor, expected):
    repo.upsert(lead("INV/1", inwestor=investor, **DYWITY, **decided(5)))
    open_card(bot, api, repo, "INV/1")
    bot.handle_update(click(MIETEK, f"d:{repo.get('INV/1').nr}"))
    details = api.edits[-1]["text"]
    assert (expected in details) if expected else "Inwestor" not in details
    assert not any(word in details.lower() for word in ("osoba fizyczna", "prywat", "niejawny", "brak"))


@pytest.mark.parametrize("geo, expected", [
    (dict(precyzja_geo="dzialka", lat=53.8, lon=20.4), "🗺️ Mapa: lokalizacja dokładna – działka"),
    (dict(precyzja_geo="obreb", lat=53.8, lon=20.4), "🗺️ Mapa: lokalizacja przybliżona – środek obrębu"),
    (dict(precyzja_geo=None, lat=None, lon=None, google_maps_url=None), None),  # bez mapy – bez linii „Mapa: brak”
])
def test_location_accuracy_is_stated_whenever_there_is_a_map(bot, api, repo, geo, expected):
    repo.upsert(lead("GEO/1", **{**DYWITY, **geo}, **decided(5)))
    card = open_card(bot, api, repo, "GEO/1")
    assert (expected in card["text"]) if expected else "🗺️" not in card["text"]
    assert ("📍 Mapa" in labels(card["markup"])) == (geo.get("lat") is not None)


# --- Alert czytelny w 3 sekundy: co, gdzie, kto, kontakt, kiedy, źródło – tylko pola z danymi ----------------

def instant_alert(bot, api, repo, inv):
    BotStore(repo).set_mode(MIETEK, "natychmiast")
    repo.upsert(inv)
    assert bot.deliver_instant() == 1
    return api.last_to(MIETEK)


def test_instant_alert_shows_investor_contact_and_case_number_in_reading_order(bot, api, repo):
    alert = instant_alert(bot, api, repo, lead(
        "ST-WM-OS/WNIOSEK/1049/2026", nazwa_zamierzenia="Budowa budynku usługowego", kategoria="komercyjna",
        inwestor="XYZ Sp. z o.o.", telefon="+48600123456", email="biuro@xyz.pl", **DYWITY, **decided(2)))
    text = alert["text"]

    assert text.startswith("🏭 <b>Obiekt komercyjny (hala, sklep, biuro)</b> · pozwolenie na budowę")
    lines = ["<b>Budowa budynku usługowego</b>", "📍 Dywity, ul. Polna 5", "💼 Inwestor: XYZ Sp. z o.o.",
             "📞 Kontakt wpisany w rejestrze: +48 600 123 456 · biuro@xyz.pl", "📅 Decyzja: 27.09.2026",
             "🔖 Nr sprawy: <code>ST-WM-OS/WNIOSEK/1049/2026</code>"]
    positions = [text.index(line) for line in lines]
    assert positions == sorted(positions)
    assert "📍 Mapa" in labels(alert["markup"])


@pytest.mark.parametrize("contact, expected", [
    (dict(telefon="+48600123456"), "📞 Kontakt wpisany w rejestrze: +48 600 123 456"),
    (dict(email="biuro@xyz.pl"), "✉️ Kontakt wpisany w rejestrze: biuro@xyz.pl"),
])
def test_contact_from_the_register_is_shown_also_in_details(bot, api, repo, contact, expected):
    repo.upsert(lead("TEL/1", **contact, **DYWITY, **decided(5)))
    assert expected in open_card(bot, api, repo, "TEL/1")["text"]
    bot.handle_update(click(MIETEK, f"d:{repo.get('TEL/1').nr}"))
    assert expected in api.edits[-1]["text"]


def test_alert_leaves_out_what_the_register_does_not_have(bot, api, repo):
    alert = instant_alert(bot, api, repo, lead(
        "PUSTA/1", nazwa_zamierzenia=None, adres_opisowy=None, miejscowosc=None, gmina="Dywity", inwestor=None,
        data_decyzji=None, data_wplywu=None, lat=None, lon=None, precyzja_geo=None, google_maps_url=None))
    text = alert["text"]

    assert "📍 gm. Dywity" in text
    assert "brak" not in text.lower()
    for absent in ("💼", "📞", "✉️", "📅", "🗺️", "<b></b>"):
        assert absent not in text, absent
    assert "🔖 Nr sprawy: <code>PUSTA/1</code>" in text

    bot.handle_update(click(MIETEK, f"d:{repo.get('PUSTA/1').nr}"))
    assert "brak" not in api.edits[-1]["text"].lower()

    bot.handle_update(message(MIETEK, "📊 Inwestycje"))  # na liście: rodzaj zamiast „(brak opisu)”
    listing = api.last_to(MIETEK)["text"]
    assert "1. 🏠 <b>Dom jednorodzinny</b>" in listing and "brak" not in listing.lower()


def test_facts_estimates_and_own_work_are_separate(bot, api, repo):
    store = BotStore(repo)
    store.set_filters(MIETEK, UserFilters(baza=BASE, promien_km=20))
    store.set_trade(MIETEK, "dach")
    repo.upsert(lead("SEP/1", **DYWITY, **decided(140)))
    store.set_note(MIETEK, "SEP/1", "Kierownik budowy: pan Adam")

    text = open_card(bot, api, repo, "SEP/1")["text"]

    facts, estimates, own = (text.index(h) for h in ("📋 <b>Z rejestru GUNB</b>", "📐 <b>Szacunki</b>",
                                                       "👤 <b>Twoje</b>"))
    assert facts < estimates < own
    assert text.index("📏 6 km w linii prostej od Twojej bazy") > estimates
    assert "⏳ Etap dachu: orientacyjnie" in text and "trzeba sprawdzić na miejscu" in text
    assert text.index("📝 Twoja notatka: Kierownik budowy: pan Adam") > own


def test_card_of_another_person_shows_no_private_note(bot, api, repo):
    activate(bot, api, OBCY)
    repo.upsert(lead("PRV/1", **DYWITY, **decided(5)))
    BotStore(repo).set_note(MIETEK, "PRV/1", "tajne ustalenia")
    assert "tajne" not in open_card(bot, api, repo, "PRV/1", chat_id=OBCY)["text"]


# --- Kolejność -------------------------------------------------------------------------------------------

def test_report_puts_the_nearer_area_first_and_size_does_not_jump_the_queue(bot, api, repo):
    BotStore(repo).set_filters(MIETEK, UserFilters(baza=BASE, promien_km=50))
    repo.upsert(lead("O/1", nazwa_zamierzenia="Wielki blok daleko", priorytet="hot", punkty=12, kubatura=30000.0,
                     lat=54.05, lon=20.48, **decided(1)))  # ok. 30 km, najnowszy i największy
    repo.upsert(lead("O/2", nazwa_zamierzenia="Dom blisko, starszy", lat=53.80, lon=20.49, **decided(9)))
    repo.upsert(lead("O/3", nazwa_zamierzenia="Dom blisko, nowszy", lat=53.79, lon=20.50, **decided(2)))

    bot.handle_update(message(MIETEK, "📊 Inwestycje"))
    text = api.last_to(MIETEK)["text"]
    order = [text.index(name) for name in ("Dom blisko, nowszy", "Dom blisko, starszy", "Wielki blok daleko")]
    assert order == sorted(order)


# --- Wynik pracy (P1) --------------------------------------------------------------------------------------

def test_outcome_is_set_from_the_card_and_repeated_clicks_change_nothing(bot, api, repo):
    repo.upsert(lead("W/1", **DYWITY, **decided(5)))
    nr = repo.get("W/1").nr
    open_card(bot, api, repo, "W/1")

    bot.handle_update(click(MIETEK, f"w:{nr}"))
    assert "💬 Rozmowa" in labels(api.edits[-1]["markup"])
    bot.handle_update(click(MIETEK, f"w:{nr}:r"))
    bot.handle_update(click(MIETEK, f"w:{nr}:r"))

    store = BotStore(repo)
    assert store.outcome(MIETEK, "W/1") == Outcome(wynik="rozmowa")
    assert "📋 Wynik: rozmowa ✓" in labels(api.edits[-1]["markup"])
    assert len([e for e in store.events(kinds=("wynik",)) if e.chat_id == MIETEK]) == 1


def test_not_matching_asks_for_a_short_reason(bot, api, repo):
    repo.upsert(lead("W/2", **DYWITY, **decided(5)))
    nr = repo.get("W/2").nr
    bot.handle_update(click(MIETEK, f"w:{nr}:n"))
    assert "📍 Zły obszar" in labels(api.edits[-1]["markup"])
    bot.handle_update(click(MIETEK, f"wp:{nr}:a"))
    assert BotStore(repo).outcome(MIETEK, "W/2") == Outcome(wynik="niepasujaca", powod="obszar")


def test_thumbs_are_one_rating_per_person_and_investment(bot, api, repo):
    activate(bot, api, OBCY)
    repo.upsert(lead("W/3", **DYWITY, **decided(5)))
    nr = repo.get("W/3").nr
    for _ in range(3):
        bot.handle_update(click(MIETEK, f"fu:{nr}:1"))  # także stare przyciski z historii czatu
    bot.handle_update(click(OBCY, f"fu:{nr}:0"))
    store = BotStore(repo)
    assert store.outcome(MIETEK, "W/3").ocena == 1 and store.outcome(OBCY, "W/3").ocena == -1
    assert len(store.events(kinds=("przydatne",))) == 1


def test_expired_access_keeps_own_work_readable_but_old_buttons_show_nothing_new(bot, api, repo, clock):
    activate(bot, api, OBCY)
    store = BotStore(repo)
    repo.upsert(lead("ARCH/1", nazwa_zamierzenia="Zapisana budowa", **DYWITY, **decided(5)))
    repo.upsert(lead("ARCH/2", nazwa_zamierzenia="Niezapisana budowa", **DYWITY, **decided(5)))
    saved_nr, other_nr = repo.get("ARCH/1").nr, repo.get("ARCH/2").nr
    bot.handle_update(click(MIETEK, f"s1:{saved_nr}"))
    store.set_note(MIETEK, "ARCH/1", "oddzwonić")
    clock.advance(days=31)  # dostęp 30 dni minął
    api.sent.clear()
    api.answers.clear()

    bot.handle_update(message(MIETEK, "⭐ Zapisane"))
    saved = api.last_to(MIETEK)["text"]
    bot.handle_update(click(MIETEK, f"o:{saved_nr}"))
    archive = api.last_to(MIETEK)
    bot.handle_update(click(MIETEK, f"o:{other_nr}"))
    bot.handle_update(click(MIETEK, f"d:{saved_nr}"))

    assert "Zapisana budowa" in saved and "nieaktywny" in saved
    assert "Zapisana budowa" in archive["text"] and "📝 Twoja notatka: oddzwonić" in archive["text"]
    assert "🗄️" in archive["text"] and "Sprawa" not in archive["text"]
    assert {"⭐ Zapisz", "📋 Wynik", "🔽 Szczegóły"}.isdisjoint(labels(archive["markup"]))
    assert all("Niezapisana" not in m["text"] for m in api.to(MIETEK))
    assert api.answers[-2].startswith("⛔") and api.answers[-1].startswith("⛔")
