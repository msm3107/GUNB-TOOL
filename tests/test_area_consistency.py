"""Spójność nazw obszarów i kodów: etykieta powiatu z nazwy dominującej w danych (nie alfabetycznie), a działka
znaleziona w innej gminie niż podaje rejestr to lokalizacja niepewna – pokazujemy to, nie poprawiamy zgadywaniem."""

import pytest

from gunb_tool.bot_store import BotStore, UserFilters, location_mismatch
from tests.bot_helpers import ADMIN, MIETEK, activate, buttons, click, lead, make_bot, message

BASE = (53.7784, 20.4801)
DYWITY_OK = dict(gmina_teryt="2814042", teryt_dzialki="281404_2.0005.12/3", lat=53.8285, lon=20.4867,
                 precyzja_geo="dzialka", gmina="Dywity", powiat="powiat olsztyński", powiat_teryt="2814")
DYWITY_ELSEWHERE = dict(gmina_teryt="2814042", teryt_dzialki="281705_2.0012.9/3", lat=53.507, lon=21.377,
                        precyzja_geo="dzialka", gmina="Rozogi", powiat="powiat szczycieński", powiat_teryt="2814",
                        miejscowosc="Lipniak", adres_opisowy="Lipniak")


def test_powiat_label_comes_from_the_dominant_name_not_the_alphabet(repo):
    for n in range(5):
        repo.upsert(lead(f"P/{n}", powiat="powiat olsztyński", powiat_teryt="2814"))
        repo.upsert(lead(f"M/{n}", powiat="powiat Olsztyn", powiat_teryt="2862"))
    repo.upsert(lead("X/1", powiat="powiat szczycieński", powiat_teryt="2814"))  # pojedynczy rozjazd z ULDK
    repo.upsert(lead("X/2", powiat="powiat olsztyński", powiat_teryt="2862"))
    assert dict(BotStore(repo).place_options(("2862", "2814"))) == {"2862": "Olsztyn", "2814": "powiat olsztyński"}


def test_parcel_found_in_another_gmina_is_an_uncertain_location():
    assert location_mismatch(lead("A", **DYWITY_ELSEWHERE))
    assert not location_mismatch(lead("B", **DYWITY_OK))
    assert not location_mismatch(lead("C", gmina_teryt=None, teryt_dzialki="281705_2.0012.9/3"))
    assert UserFilters(baza=BASE, promien_km=100).distance_km(lead("A", **DYWITY_ELSEWHERE)) is None


@pytest.fixture
def bot(repo, api, clock):
    bot = make_bot(repo, api, clock)
    activate(bot, api)
    return bot


def test_card_says_the_location_is_uncertain_and_hides_the_distance(bot, api, repo):
    BotStore(repo).set_filters(MIETEK, UserFilters(baza=BASE))  # baza bez promienia – odległości na liście
    repo.upsert(lead("U/1", nazwa_zamierzenia="Dom z niepewną działką", **DYWITY_ELSEWHERE))
    bot.handle_update(click(MIETEK, f"o:{repo.get('U/1').nr}"))
    card = api.last_to(MIETEK)
    assert "🗺️ Mapa: lokalizacja niepewna – działka znaleziona w innej gminie niż podaje rejestr" in card["text"]
    assert "📏" not in card["text"]
    assert "📍 Mapa (niepewna)" in [text for text, _ in buttons(card["markup"])]


def test_data_report_counts_uncertain_locations(bot, api, repo):
    repo.upsert(lead("U/1", **DYWITY_ELSEWHERE))
    repo.upsert(lead("U/2", **DYWITY_OK))
    bot.handle_update(message(ADMIN, "/start"))
    bot.handle_update(message(ADMIN, "/dane"))
    assert "Lokalizacja niepewna (działka poza gminą z rejestru): 1" in api.last_to(ADMIN)["text"]


def test_data_report_fits_one_message_even_with_many_name_variants(bot, api, repo):
    for n in range(60):
        for name in ("Gmina A " * 8, "Gmina B " * 8):
            repo.upsert(lead(f"V/{n}/{name[6]}", gmina_teryt=f"28{n:05d}", gmina=name))
    bot.handle_update(message(ADMIN, "/start"))
    bot.handle_update(message(ADMIN, "/dane"))
    text = api.last_to(ADMIN)["text"]
    assert len(text) <= 4096 and "kolejnych kodów gmin" in text
