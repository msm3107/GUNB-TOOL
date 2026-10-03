"""P0: zrozumiałe wejście – nowa osoba w trybie approval dowiaduje się, co to jest, dla kogo, jaki obszar,
jak wygląda wynik i jak zacząć; nikt nie sugeruje jej zaległości w płatności; przykład jest syntetyczny."""

from decimal import Decimal

import pytest

from gunb_tool.bot_store import BotStore
from gunb_tool.config import OfferConfig
from tests.bot_helpers import ADMIN, MIETEK, OBCY, buttons, click, lead, make_bot, message

AREA = "Olsztyn i powiat olsztyński"
COMPLETE = OfferConfig(area=AREA, price=Decimal("99"), tax="netto_vat", vat_rate=23, period_days=30,
                       payment="przelew – dane w potwierdzeniu zamówienia", seller_name="Jan Przykładowy",
                       seller_contact="@jan_przyklad", response_time="w dni robocze 16:00–20:00")
PAYMENT_PRESSURE = ("opłac", "abonament", "nieaktywny", "zaległ", "⛔")


@pytest.fixture
def bot(repo, api, clock):
    return make_bot(repo, api, clock, offer=OfferConfig(area=AREA))


def user(repo, chat_id=MIETEK):
    return BotStore(repo).get_user(chat_id)


def labels(markup):
    return [text for text, _ in buttons(markup)]


# --- Pierwszy kontakt -----------------------------------------------------------------------------------------

def test_first_contact_explains_the_product_and_the_way_in(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start"))

    intro = api.last_to(MIETEK)
    text = intro["text"]
    assert "<b>Żółta Tablica</b>" in text and "rejestr" in text
    assert "hurtownie i składy budowlane" in text and f"📍 Obszar: {AREA}" in text
    assert "pilotaż z ręcznym uruchomieniem" in text and "7-dniowy test" in text
    assert "nie zamówienia" in text  # lista budów do sprawdzenia, nie gotowi klienci
    assert not any(word in text.lower() for word in PAYMENT_PRESSURE)
    assert {"👀 Zobacz przykład", "🙋 Chcę przetestować", "💳 Oferta i cena", "❓ Jak to działa"} <= set(labels(intro["markup"]))
    assert "zł" not in text and "zapytaj" in text  # oferta niepełna – bez wymyślonej ceny
    assert "🕒" not in text  # czas odpowiedzi tylko, gdy operator go ustawił
    assert "🆕 Nowa osoba" in api.last_to(ADMIN)["text"]


def test_price_and_response_time_appear_only_when_configured(repo, api, clock):
    bot = make_bot(repo, api, clock, offer=COMPLETE)
    bot.handle_update(message(MIETEK, "/start"))
    text = api.last_to(MIETEK)["text"]
    assert "💳 Po teście: 99 zł netto + 23% VAT = 121,77 zł do zapłaty za 30 dni" in text
    assert "🕒 Odpowiadam: w dni robocze 16:00–20:00" in text


def test_menu_command_without_access_gets_a_friendly_gate(bot, api):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(MIETEK, "/nowe"))
    gate = api.last_to(MIETEK)
    assert gate["text"].startswith("🔒") and not any(w in gate["text"].lower() for w in ("opłac", "zaległ"))
    assert "🙋 Chcę przetestować" in labels(gate["markup"])


# --- Przykład, pomoc, oferta przed dostępem --------------------------------------------------------------

def test_demo_is_synthetic_clearly_marked_and_reveals_no_records(bot, api, repo):
    repo.upsert(lead("REAL/1", nazwa_zamierzenia="Prawdziwa inwestycja z bazy", miejscowosc="Dywity"))
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(click(MIETEK, "i:demo"))

    demo = api.last_to(MIETEK)
    assert "PRZYKŁAD – dane fikcyjne" in demo["text"]
    assert "Prawdziwa inwestycja" not in demo["text"] and "Dywity" not in demo["text"]
    assert all(not data.startswith("o:") for _, data in buttons(demo["markup"]))  # bez numerów do realnych kart
    assert "🙋 Chcę przetestować" in labels(demo["markup"])


def test_help_and_data_limits_are_available_before_any_access(bot, api):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(click(MIETEK, "i:pomoc"))
    text = api.last_to(MIETEK)["text"]
    for fact in ("tylko sprawy zakończone pozytywnie", "z opóźnieniem", "szacunki", AREA,
                 "nie ma danych kontaktowych inwestorów", "kontakt pokażę tylko, gdy ktoś wpisał go w rejestr"):
        assert fact in text
    assert "nie ma gwarancji zlecenia" in text  # wprost: bez obietnic


def test_incomplete_offer_shows_an_honest_inquiry_path_only(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(click(MIETEK, "i:oferta"))
    offer = api.last_to(MIETEK)
    assert "zł" not in offer["text"] and "💬 Zapytaj o ofertę" in labels(offer["markup"])
    assert "🛒 Zamawiam" not in labels(offer["markup"])

    bot.handle_update(click(MIETEK, "zm:new"))  # stary/nieprzewidziany przycisk zamówienia – też tylko pytanie
    assert repo.connection.execute("SELECT COUNT(*) FROM zamowienia").fetchone()[0] == 0
    assert "💬 Pytanie o ofertę" in api.last_to(ADMIN)["text"]


def test_complete_offer_lists_price_seller_rules_and_order_button(repo, api, clock):
    bot = make_bot(repo, api, clock, offer=COMPLETE)
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(click(MIETEK, "i:oferta"))
    offer = api.last_to(MIETEK)
    for part in ("99 zł netto + 23% VAT = 121,77 zł do zapłaty", "30 dni", "Jan Przykładowy", "@jan_przyklad",
                 "przelew", AREA, "1 konto Telegram"):
        assert part in offer["text"]
    assert {"🛒 Zamawiam", "💬 Pytanie"} <= set(labels(offer["markup"]))


# --- Prośba o test, odmowa, źródło ---------------------------------------------------------------------

def test_trial_request_reaches_the_admin_once(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start strona"))
    api.sent.clear()

    bot.handle_update(click(MIETEK, "i:test"))
    bot.handle_update(click(MIETEK, "i:test"))

    requests = [m for m in api.to(ADMIN) if m["text"].startswith("🙋 Prośba o test")]
    assert len(requests) == 1 and "źródło: strona" in requests[0]["text"]
    assert ("🎁 Test 7 dni", f"adm:trial:{MIETEK}") in buttons(requests[0]["markup"])
    assert "Prośba o test wysłana" in api.last_to(MIETEK)["text"]
    assert api.answers[-1].startswith("⏳ Prośba o test jest już wysłana")
    assert user(repo).prosba_o_test is not None

    bot.handle_update(click(ADMIN, f"adm:trial:{MIETEK}"))  # admin pozwala – osoba ustawia branżę i obszar
    assert "1/2" in api.last_to(MIETEK)["text"]


@pytest.mark.parametrize("payload, saved", [("strona", "strona"), ("FB_wrzesien", "fb_wrzesien"),
                                            ("600123456", "inne"), ("jan.kowalski@x.pl", "inne"),
                                            ("a" * 40, "inne")])
def test_start_parameter_is_validated_and_never_stores_free_text(bot, api, repo, payload, saved):
    bot.handle_update(message(MIETEK, f"/start {payload}"))
    assert user(repo).zrodlo == saved


def test_source_is_the_first_touch(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start ulotka"))
    bot.handle_update(message(MIETEK, "/start strona"))
    assert user(repo).zrodlo == "ulotka"
    assert [e.szczegoly for e in BotStore(repo).events(kinds=("start",))] == ["ulotka"]


def test_refusal_is_polite_and_closes_the_door(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(click(MIETEK, "i:test"))
    bot.handle_update(click(ADMIN, f"adm:no:{MIETEK}"))

    refusal = api.last_to(MIETEK)["text"]
    assert "Dziękujemy za zainteresowanie" in refusal and "@admin_gunb" in refusal
    bot.handle_update(click(MIETEK, "i:test"))
    assert api.answers[-1].startswith("⛔")
    assert user(repo).status == "odrzucony"


def test_people_who_already_have_access_are_not_asked_to_request_a_test(bot, api, repo):
    bot.handle_update(message(OBCY, "/start"))
    bot.handle_update(click(ADMIN, f"adm:ok:{OBCY}"))
    bot.handle_update(click(OBCY, "i:test"))
    assert api.answers[-1].startswith("✅ Masz już dostęp")
    assert not any(m["text"].startswith("🙋") for m in api.to(ADMIN))
