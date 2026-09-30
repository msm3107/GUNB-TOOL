"""P1.1: pierwsze kroki (branża → obszar → podsumowanie i start testu) oraz proste menu."""

import pytest

from gunb_tool.bot import MENU_BUTTONS
from gunb_tool.bot_store import BotStore, UserFilters
from gunb_tool.storage import LeadRepository
from tests.bot_helpers import ADMIN, MIETEK, activate, buttons, click, lead, make_bot, message

OLSZTYN = dict(miejscowosc="Olsztyn", gmina="Olsztyn", adres_opisowy="Olsztyn", powiat="powiat Olsztyn",
               powiat_teryt="1465", gmina_teryt="1465011", lat=53.7784, lon=20.4801)


@pytest.fixture
def bot(repo, api, clock):
    bot = make_bot(repo, api, clock)
    repo.upsert(lead("OLS/1", nazwa_zamierzenia="Dom w Olsztynie", **OLSZTYN))
    return bot


def allowed(bot, api):
    """Nowa osoba, admin pozwala na test – zaczyna się prowadzenie krok po kroku."""
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(click(ADMIN, f"adm:trial:{MIETEK}"))


def user(repo):
    return BotStore(repo).get_user(MIETEK)


def test_new_user_is_guided_trade_then_area_then_summary_with_trial_start(bot, api, repo):
    allowed(bot, api)

    step1 = api.last_to(MIETEK)
    assert "1/2" in step1["text"] and ("🏠 Dach", "ob:dach") in buttons(step1["markup"])

    bot.handle_update(click(MIETEK, "ob:dach"))
    step2 = api.edits[-1]
    assert "2/2" in step2["text"] and "Monitoruję" in step2["text"]
    assert ("🗺️ Cały monitorowany obszar", "oa:all") in buttons(step2["markup"])
    assert user(repo).branza == "dach"

    bot.handle_update(click(MIETEK, "oa:all"))
    summary = api.edits[-1]
    assert "Gotowe" in summary["text"] and "Dach" in summary["text"]
    assert "cały monitorowany obszar" in summary["text"] and "rano o 07:00" in summary["text"]
    assert ("▶️ Zacznij 7-dniowy test", "ts") in buttons(summary["markup"])
    assert user(repo).test_start is None  # samo ustawienie nie uruchamia testu

    bot.handle_update(click(MIETEK, "ts"))

    texts = [m["text"] for m in api.to(MIETEK)]
    assert any(t.startswith("🎁 Aktywowano darmowy okres próbny na 7 dni!") for t in texts)
    history = api.last_to(MIETEK)["text"]
    assert "ostatnich 30 dni" in history and "Dom w Olsztynie" in history


def test_area_can_be_set_without_location(bot, api, repo):
    allowed(bot, api)
    bot.handle_update(click(MIETEK, "ob:none"))
    bot.handle_update(click(MIETEK, "oa:p:1465"))
    assert user(repo).filtry.powiaty == ("1465",)
    assert "Gotowe" in api.edits[-1]["text"]


def test_area_buttons_work_when_the_whole_voivodeship_is_monitored(bot, api, repo):
    """Bez listy powiatów w konfiguracji przyciski to powiaty z danych – kliknięty też musi zadziałać."""
    bot.powiat_codes = ()
    allowed(bot, api)
    bot.handle_update(click(MIETEK, "ob:none"))
    assert ("📌 Olsztyn", "oa:p:1465") in buttons(api.edits[-1]["markup"])

    bot.handle_update(click(MIETEK, "oa:p:1465"))

    assert user(repo).filtry.powiaty == ("1465",)
    assert "Gotowe" in api.edits[-1]["text"]


def test_typed_place_outside_the_monitored_area_is_explained(bot, api, repo):
    allowed(bot, api)
    bot.handle_update(click(MIETEK, "ob:none"))
    bot.handle_update(click(MIETEK, "oa:txt"))
    bot.handle_update(message(MIETEK, "Gdańsk"))

    reply = api.last_to(MIETEK)["text"]
    assert "Gdańsk" in reply and "monitorowanym obszarze" in reply
    assert user(repo).filtry.miejsca == ()  # nieznanej nazwy nie dopisujemy
    bot.handle_update(message(MIETEK, "Olsztyn"))  # można od razu wpisać inną
    assert user(repo).filtry.miejsca == ("Olsztyn",)
    assert "Gotowe" in api.last_to(MIETEK)["text"]


def test_location_far_from_the_monitored_area_is_explained(bot, api, repo):
    allowed(bot, api)
    bot.handle_update(click(MIETEK, "ob:none"))
    bot.handle_update(click(MIETEK, "oa:loc"))
    pin = message(MIETEK, "")
    pin["message"]["location"] = {"latitude": 54.352, "longitude": 18.646}  # Gdańsk
    bot.handle_update(pin)

    texts = " ".join(m["text"] for m in api.to(MIETEK)[-3:])
    assert "poza monitorowanym obszarem" in texts


def test_onboarding_resumes_where_it_stopped(bot, api, repo):
    allowed(bot, api)
    bot.handle_update(click(MIETEK, "ob:okna"))

    bot.handle_update(message(MIETEK, "/start"))  # wrócił po przerwie

    assert "2/2" in api.last_to(MIETEK)["text"]


def test_trial_start_before_setup_brings_the_missing_step(bot, api, repo):
    allowed(bot, api)
    bot.handle_update(click(MIETEK, "ts"))
    assert user(repo).test_start is None
    assert "Najpierw" in api.answers[-1]
    assert "1/2" in api.last_to(MIETEK)["text"]


def test_account_screen_asks_to_finish_setup_before_start(bot, api, repo):
    allowed(bot, api)
    bot.handle_update(message(MIETEK, "/konto"))
    assert ("⚙️ Dokończ ustawienia", "ob:resume") in buttons(api.last_to(MIETEK)["markup"])


def test_history_review_does_not_use_up_new_notifications(bot, api, repo, clock):
    allowed(bot, api)
    bot.handle_update(click(MIETEK, "ob:none"))
    bot.handle_update(click(MIETEK, "oa:all"))
    bot.handle_update(click(MIETEK, "ts"))
    api.sent.clear()

    bot.deliver_reports("rano")

    assert "Dom w Olsztynie" in api.last_to(MIETEK)["text"]  # w raporcie nadal jako nowość


def test_no_matches_after_setup_show_filters_and_simple_actions(bot, api, repo):
    allowed(bot, api)
    bot.handle_update(click(MIETEK, "ob:none"))
    bot.handle_update(click(MIETEK, "oa:p:3021"))  # powiat bez żadnej inwestycji w bazie
    bot.handle_update(click(MIETEK, "ts"))

    history = api.last_to(MIETEK)
    assert "brak pasujących" in history["text"]
    assert {"🗺️ Poszerz obszar", "🏗️ Zmień rodzaj"} <= {t for t, _ in buttons(history["markup"])}
    assert user(repo).filtry.powiaty == ("3021",)  # filtrów nie czyścimy sami


def test_paid_access_for_a_new_user_also_starts_with_setup(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(click(ADMIN, f"adm:ok:{MIETEK}"))
    assert "1/2" in api.last_to(MIETEK)["text"]
    bot.handle_update(click(MIETEK, "ob:none"))
    bot.handle_update(click(MIETEK, "oa:all"))
    assert "ostatnich 30 dni" in api.last_to(MIETEK)["text"]  # od razu przegląd, bez przycisku testu


def test_existing_users_do_not_repeat_setup_after_update(tmp_path, api, clock):
    from tests.test_storage import legacy_database

    path = tmp_path / "v9.sqlite"
    legacy = legacy_database(path, 9)
    legacy.execute("INSERT INTO bot_users (chat_id, status, nowe_od, utworzono, zmieniono, dostep_bez_limitu)"
                   " VALUES (?, 'aktywny', 'x', 'x', 'x', 1)", (MIETEK,))
    legacy.commit()
    legacy.close()

    with LeadRepository(path, now=clock.now_utc) as repo:
        make_bot(repo, api, clock).handle_update(message(MIETEK, "/start"))
    assert "1/2" not in api.last_to(MIETEK)["text"]
    assert api.last_to(MIETEK)["markup"]["keyboard"]


# --- Menu -----------------------------------------------------------------------------------------

def test_main_menu_has_four_big_buttons(bot, api):
    bot.handle_update(message(ADMIN, "/start"))
    welcome = next(m for m in api.to(ADMIN) if "keyboard" in (m["markup"] or {}))
    keyboard = [b["text"] for row in welcome["markup"]["keyboard"] for b in row]
    assert keyboard == list(MENU_BUTTONS) == ["📊 Inwestycje", "⭐ Zapisane", "⚙️ Ustawienia", "❓ Pomoc"]


@pytest.mark.parametrize("old, expected", [("🔎 Filtry", "Twoje filtry"), ("⏰ Kiedy wysyłać", "Kiedy mam wysyłać"),
                                           ("📊 Co nowego?", "Raport"), ("/filtry", "Twoje filtry"),
                                           ("📊 Inwestycje", "Raport")])
def test_old_buttons_and_commands_still_work(bot, api, old, expected):
    activate(bot, api, ADMIN)
    bot.handle_update(message(ADMIN, old))
    assert expected in api.last_to(ADMIN)["text"]


def test_settings_lead_to_filters_trade_schedule_and_account(bot, api):
    activate(bot, api, ADMIN)
    bot.handle_update(message(ADMIN, "⚙️ Ustawienia"))
    hub = api.last_to(ADMIN)
    labels = {t for t, _ in buttons(hub["markup"])}
    assert {"🔎 Obszar i rodzaj", "🧰 Branża", "👀 Obserwowane", "⏰ Harmonogram", "👤 Konto"} <= labels

    bot.handle_update(click(ADMIN, "st:f", message_id=hub["message_id"]))
    assert "Twoje filtry" in api.edits[-1]["text"]
    bot.handle_update(click(ADMIN, "st:k", message_id=hub["message_id"]))
    assert "Twoje konto" in api.edits[-1]["text"]


def test_old_setup_buttons_after_setup_do_not_restart_it(bot, api, repo):
    allowed(bot, api)
    bot.handle_update(click(MIETEK, "ob:none"))
    bot.handle_update(click(MIETEK, "oa:p:1465"))
    BotStore(repo).set_filters(MIETEK, UserFilters(powiaty=("1465",), miejsca=("Dywity",)))

    bot.handle_update(click(MIETEK, "ob:dach"))  # stary przycisk z kroku 1/2 w historii czatu
    bot.handle_update(click(MIETEK, "oa:all"))  # i z kroku 2/2

    assert user(repo).setup_done
    assert user(repo).filtry == UserFilters(powiaty=("1465",), miejsca=("Dywity",))
    assert "⚙️ Ustawienia" in api.answers[-1]


def test_typed_place_is_accepted_while_there_is_no_data_yet(repo, api, clock):
    bot = make_bot(repo, api, clock)  # świeża instalacja – w bazie jeszcze żadnej inwestycji
    allowed(bot, api)
    bot.handle_update(click(MIETEK, "ob:none"))
    bot.handle_update(click(MIETEK, "oa:txt"))
    bot.handle_update(message(MIETEK, "Dywity"))
    assert user(repo).filtry.miejsca == ("Dywity",)
    assert "Gotowe" in api.last_to(MIETEK)["text"]
