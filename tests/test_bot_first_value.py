"""P0: onboarding i pierwsza wartość – dwa pytania, podsumowanie z zakresem danych, liczbą pasujących
i świeżością; pusty wynik nazwany po przyczynie; test nigdy nie startuje sam; po starcie 3–5 najlepszych
pozycji i jednorazowe podpowiedzi."""

from datetime import date, timedelta

import pytest

from gunb_tool.bot import IMPORT_JOB, LAST_IMPORT_JOB
from gunb_tool.bot_store import BotStore
from gunb_tool.config import OfferConfig
from tests.bot_helpers import ADMIN, MIETEK, buttons, click, lead, make_bot, message

TODAY = date(2026, 9, 29)
DYWITY = dict(adres_opisowy="Dywity", miejscowosc="Dywity", gmina="Dywity", powiat="olsztyński",
              powiat_teryt="1465", gmina_teryt="1465032", lat=53.8285, lon=20.4867)


def decided(days_ago: int) -> dict:
    day = (TODAY - timedelta(days=days_ago)).isoformat()
    return dict(data_decyzji=day, data_aktualizacji=day)


@pytest.fixture
def bot(repo, api, clock):
    return make_bot(repo, api, clock, offer=OfferConfig(area="Olsztyn i powiat olsztyński"))


def allowed(bot, api):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(click(ADMIN, f"adm:trial:{MIETEK}"))


def setup(bot, trade="none", area="oa:all"):
    bot.handle_update(click(MIETEK, f"ob:{trade}"))
    bot.handle_update(click(MIETEK, area))


def store(repo):
    return BotStore(repo)


def labels(markup):
    return [text for text, _ in buttons(markup)]


def summary_message(api):
    return next(m for m in reversed(api.edits + api.sent) if m["chat_id"] == MIETEK and m["text"]
                and "Gotowe" in m["text"])


# --- Dwa pytania ------------------------------------------------------------------------------------------

def test_first_question_is_what_you_offer_and_does_not_narrow_building_types(bot, api, repo):
    allowed(bot, api)
    step = api.last_to(MIETEK)
    assert "1/2" in step["text"] and "Co oferujesz?" in step["text"]
    assert "nie zawęża rodzaju budynków" in step["text"]
    assert ("🏪 Materiały budowlane (skład, hurtownia)", "ob:materialy") in buttons(step["markup"])

    bot.handle_update(click(MIETEK, "ob:materialy"))
    assert "Gdzie działasz?" in api.edits[-1]["text"]
    assert store(repo).get_user(MIETEK).filtry.kategorie == ()


# --- Podsumowanie przed testem ---------------------------------------------------------------------------

def test_summary_shows_settings_data_range_freshness_and_matching_count(bot, api, repo, clock):
    for n, days in enumerate((3, 12, 200)):
        repo.upsert(lead(f"D/{n}", nazwa_zamierzenia=f"Dom {n}", **DYWITY, **decided(days)))
    store(repo).set_job_time(LAST_IMPORT_JOB, clock.now_utc())
    allowed(bot, api)
    setup(bot, trade="dach", area="oa:txt")
    bot.handle_update(message(MIETEK, "Dywity"))

    summary = summary_message(api)
    text = summary["text"]
    assert "🧰 Oferujesz: 🏠 Dach" in text and "📍 Obszar: Dywity" in text
    assert "🏗️ Rodzaj budynków: wszystkie" in text and "zmienisz w ⚙️ Ustawienia" in text
    assert "📦 Dane: rejestr GUNB (pozwolenia i zgłoszenia), Olsztyn i powiat olsztyński" in text
    assert "w bocie od 13.03.2026 do 26.09.2026" in text
    assert "🕒 Rejestr GUNB sprawdzony: 29.09.2026, 07:00" in text
    assert "🔎 Teraz pasuje: 2 inwestycje z ostatnich 30 dni" in text
    assert ("▶️ Zacznij 7-dniowy test", "ts") in buttons(summary["markup"])
    assert store(repo).get_user(MIETEK).test_start is None  # test nie startuje sam


@pytest.mark.parametrize("case, expected", [
    ("brak_danych", "Nie mam jeszcze danych z rejestru"),
    ("filtry", "żadna nie pasuje do Twoich ustawień"),
    ("poza_obszarem", "poza monitorowanym obszarem"),
])
def test_empty_results_are_explained_by_their_cause(bot, api, repo, case, expected):
    if case != "brak_danych":
        repo.upsert(lead("X/1", **{**DYWITY, "powiat_teryt": "1465"}, **decided(5)))
    allowed(bot, api)
    if case == "poza_obszarem":
        bot.handle_update(click(MIETEK, "ob:none"))
        pin = message(MIETEK, "")
        pin["message"]["location"] = {"latitude": 54.352, "longitude": 18.646}  # Gdańsk
        bot.handle_update(pin)
    else:
        setup(bot, area="oa:all" if case == "brak_danych" else "oa:p:3021")

    summary = summary_message(api)
    assert expected in summary["text"]
    assert ("▶️ Zacznij 7-dniowy test", "ts") not in buttons(summary["markup"])
    assert {"▶️ Rozumiem – zacznij test mimo to", "🗺️ Zmień obszar", "💬 Napisz do nas"} <= set(labels(summary["markup"]))
    assert store(repo).get_user(MIETEK).test_start is None
    if case == "brak_danych":
        assert "nie brak budów na rynku" in summary["text"]


def test_stale_registry_is_called_out(bot, api, repo, clock):
    repo.upsert(lead("X/1", **DYWITY, **decided(5)))
    store(repo).job_finished(IMPORT_JOB, "blad", "HTTP 503")
    allowed(bot, api)
    setup(bot)
    assert "dane mogą być nieaktualne" in summary_message(api)["text"].lower()


def test_user_can_knowingly_start_despite_empty_results(bot, api, repo):
    allowed(bot, api)
    setup(bot)
    bot.handle_update(click(MIETEK, "ts:ok"))
    assert store(repo).get_user(MIETEK).test_start is not None
    first = api.last_to(MIETEK)
    assert "brak pasujących" in first["text"] and "Nie mam jeszcze danych z rejestru" in first["text"]
    assert {"🗺️ Poszerz obszar", "🏗️ Zmień rodzaj", "💬 Napisz do nas"} <= set(labels(first["markup"]))


# --- Pierwsza wartość po starcie ------------------------------------------------------------------------------

def test_after_start_the_best_five_come_first_with_a_full_review(bot, api, repo):
    for n in range(8):
        repo.upsert(lead(f"N/{n}", nazwa_zamierzenia=f"Budowa numer {n}", **DYWITY, **decided(n + 1)))
    allowed(bot, api)
    setup(bot, trade="materialy")
    bot.handle_update(click(MIETEK, "ts"))

    first = api.last_to(MIETEK)
    numbers = [data for _, data in buttons(first["markup"]) if data.startswith("o:")]
    assert len(numbers) == 5
    assert first["text"].index("Budowa numer 0") < first["text"].index("Budowa numer 4")  # najnowsze najpierw
    assert "Budowa numer 7" not in first["text"]
    assert ("📋 Pełny przegląd (8)", "hp:0") in buttons(first["markup"])
    assert "z ostatnich 30 dni" in first["text"]


def test_later_trade_sees_older_buildings_in_its_stage_window_first(bot, api, repo):
    repo.upsert(lead("OLD/1", nazwa_zamierzenia="Dom w oknie dachu", **DYWITY, **decided(150)))
    repo.upsert(lead("NEW/1", nazwa_zamierzenia="Dom z wczoraj", **DYWITY, **decided(1)))
    allowed(bot, api)
    setup(bot, trade="dach")
    bot.handle_update(click(MIETEK, "ts"))

    text = api.last_to(MIETEK)["text"]
    assert text.index("Dom w oknie dachu") < text.index("Dom z wczoraj")
    assert "orientacyjnym oknie etapu dachu" in text and "trzeba sprawdzić" in text


def test_missing_history_is_named_as_missing_data_not_as_an_empty_market(bot, api, repo):
    repo.upsert(lead("NEW/1", nazwa_zamierzenia="Dom z wczoraj", **DYWITY, **decided(1)))  # brak starszych decyzji
    allowed(bot, api)
    setup(bot, trade="elewacja")
    text = summary_message(api)["text"]
    assert "Starszych decyzji" in text and "nie brak budów na rynku" in text


# --- Podpowiedzi (samouczek) ---------------------------------------------------------------------------------

def test_tips_come_once_and_never_after_the_action_was_done(bot, api, repo):
    for n in range(3):
        repo.upsert(lead(f"T/{n}", nazwa_zamierzenia=f"Budowa {n}", **DYWITY, **decided(n + 1)))
    allowed(bot, api)
    setup(bot)
    bot.handle_update(click(MIETEK, "ts"))
    assert "💡 Kliknij numer" in api.last_to(MIETEK)["text"]

    first_nr, second_nr = repo.get("T/0").nr, repo.get("T/1").nr
    bot.handle_update(click(MIETEK, f"o:{first_nr}"))
    assert "💡 Przydatna? Kliknij ⭐ Zapisz" in api.last_to(MIETEK)["text"]
    bot.handle_update(click(MIETEK, f"o:{second_nr}"))
    assert "💡" not in api.last_to(MIETEK)["text"]

    bot.handle_update(click(MIETEK, f"s1:{first_nr}"))
    assert "💡 Do zapisanej dodaj 📝 notatkę" in api.last_to(MIETEK)["text"]
    sent = len(api.to(MIETEK))
    bot.handle_update(click(MIETEK, f"s1:{second_nr}"))
    assert len(api.to(MIETEK)) == sent  # drugi raz tej podpowiedzi nie ma

    bot.handle_update(click(MIETEK, "hp:0"))
    assert "💡 Kliknij numer" not in (api.edits[-1]["text"] or "")


def test_results_and_empty_results_are_measured(bot, api, repo):
    allowed(bot, api)
    setup(bot)
    bot.handle_update(click(MIETEK, "ts:ok"))
    repo.upsert(lead("M/1", **DYWITY, **decided(2)))
    bot.handle_update(click(MIETEK, "hp:0"))
    kinds = [(e.rodzaj, e.szczegoly) for e in store(repo).events(kinds=("wyniki", "pusto"))]
    assert kinds == [("pusto", "brak_danych"), ("wyniki", "1")]
