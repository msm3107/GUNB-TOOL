"""Zachowanie bota z perspektywy użytkownika („Pan Mietek”) – atrapa API Telegrama, prawdziwa baza SQLite."""

from datetime import datetime, timedelta, timezone

import pytest

from gunb_tool.bot import MENU_BUTTONS
from gunb_tool.bot_store import BotStore, UserFilters
from tests.bot_helpers import (ADMIN, MIETEK, OBCY, activate, buttons, callback_for, click, configured, lead,
                               make_bot, message)


@pytest.fixture
def bot(repo, api, clock):
    return make_bot(repo, api, clock)


BIG_WARSAW = dict(kategoria="mieszkaniowa-wielorodzinna", kubatura=26265.0, priorytet="hot", punkty=10,
                  nazwa_zamierzenia="Budowa zespołu dwóch budynków wielorodzinnych",
                  adres_opisowy="Warszawa", miejscowosc="Warszawa", gmina="Warszawa (miasto)",
                  powiat="powiat Warszawa", powiat_teryt="1465", gmina_teryt="1465038",
                  inwestor="Napollo 3 Sp. z o.o.")


# --- Rejestracja i menu ------------------------------------------------------------------------

def test_admin_is_active_at_once_and_gets_big_menu_buttons(bot, api):
    bot.handle_update(message(ADMIN, "/start"))
    welcome = next(m for m in api.to(ADMIN) if "keyboard" in (m["markup"] or {}))
    keyboard = [button["text"] for row in welcome["markup"]["keyboard"] for button in row]
    assert keyboard == list(MENU_BUTTONS)
    assert welcome["markup"]["resize_keyboard"] is True


GATE = "⛔ Twój dostęp jest nieaktywny. Skontaktuj się z administratorem @admin_gunb, aby opłacić abonament."
TRIAL_TEXT = ("🎁 Aktywowano darmowy okres próbny na 7 dni! Zobacz, jak szybciej docierać do klientów. "
              "Po tym czasie bot zostanie wstrzymany.")


def test_new_user_is_saved_inactive_and_told_to_contact_admin(bot, api):
    bot.handle_update(message(MIETEK, "/start"))

    user = BotStore(bot.repo).get_user(MIETEK)
    assert (user.is_active, user.subscription_ends) == (False, None)
    assert api.last_to(MIETEK)["text"] == GATE
    card = api.last_to(ADMIN)  # admin od razu wie, kogo aktywować
    assert "Mietek" in card["text"] and f"/trial {MIETEK}" in card["text"] and f"/aktywuj {MIETEK} 30" in card["text"]
    assert buttons(card["markup"]) == [("🎁 Test 7 dni", f"adm:trial:{MIETEK}"), ("✅ 30 dni", f"adm:ok:{MIETEK}"),
                                       ("⛔ Odrzuć", f"adm:no:{MIETEK}")]


@pytest.mark.parametrize("text", ["🔎 Filtry", "📊 Co nowego?", "📍 Blisko mnie", "⏰ Kiedy wysyłać", "/filtry", "/branza"])
def test_inactive_user_cannot_use_menu_or_commands(bot, api, text):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(MIETEK, text))
    assert api.last_to(MIETEK)["text"] == GATE


def test_inactive_user_cannot_use_buttons(bot, api):
    bot.handle_update(message(MIETEK, "/start"))
    api.edits.clear()
    bot.handle_update(click(MIETEK, "f:place"))
    bot.handle_update(click(MIETEK, "fb:dach"))
    assert api.edits == []
    assert BotStore(bot.repo).get_user(MIETEK).branza is None
    assert "abonament" in api.answers[-1]


def test_admin_activates_subscription_with_command(bot, api):
    bot.handle_update(message(MIETEK, "/start"))

    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 30"))

    user = BotStore(bot.repo).get_user(MIETEK)
    assert user.is_active is True
    assert user.subscription_ends == "2026-10-29T05:00:00+00:00"  # teraz + 30 dni
    welcome = next(m for m in api.to(MIETEK) if m["text"].startswith("✅ Twój abonament"))
    assert welcome["text"].startswith("✅ Twój abonament został aktywowany na 30 dni!")
    assert welcome["markup"]["keyboard"]  # od razu dostaje menu
    assert "29.10.2026" in api.last_to(ADMIN)["text"]
    bot.handle_update(message(MIETEK, "🔎 Filtry"))
    assert "Twoje filtry" in api.last_to(MIETEK)["text"]


def test_activation_extends_a_running_subscription(bot, api):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 30"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 30"))  # opłacił kolejny miesiąc przed końcem
    assert BotStore(bot.repo).get_user(MIETEK).subscription_ends == "2026-11-28T05:00:00+00:00"


def test_trial_command_works_like_the_card_button(bot, api):
    """Szczegóły 7-dniowego testu: tests/test_bot_access.py."""
    bot.handle_update(message(MIETEK, "/start"))
    configured(bot)

    bot.handle_update(message(ADMIN, f"/trial {MIETEK}"))
    bot.handle_update(click(MIETEK, "ts"))

    assert BotStore(bot.repo).get_user(MIETEK).subscription_ends == "2026-10-06T05:00:00+00:00"
    started, review = api.to(MIETEK)[-2:]  # start testu, a po nim przegląd ostatnich 30 dni
    assert started["text"].startswith(TRIAL_TEXT) and "ostatnich 30 dni" in review["text"]


def test_trial_does_not_shorten_a_paid_subscription(bot, api):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 30"))
    bot.handle_update(message(ADMIN, f"/trial {MIETEK}"))
    assert BotStore(bot.repo).get_user(MIETEK).subscription_ends == "2026-10-29T05:00:00+00:00"
    assert "ma już" in api.last_to(ADMIN)["text"]


@pytest.mark.parametrize("command", ["/aktywuj", f"/aktywuj {MIETEK}", f"/aktywuj {MIETEK} zero", "/aktywuj x 30",
                                     f"/aktywuj {MIETEK} 0", "/trial", "/trial abc"])
def test_admin_command_with_bad_arguments_shows_usage(bot, api, command):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, command))
    assert "Użycie" in api.last_to(ADMIN)["text"]
    assert BotStore(bot.repo).get_user(MIETEK).is_active is False


def test_admin_command_for_unknown_chat_is_explained(bot, api):
    bot.handle_update(message(ADMIN, "/aktywuj 5555 30"))
    assert "/start" in api.last_to(ADMIN)["text"]


def test_only_admin_can_activate(bot, api):
    activate(bot, api)
    bot.handle_update(message(OBCY, "/start"))
    bot.handle_update(message(MIETEK, f"/aktywuj {OBCY} 365"))
    bot.handle_update(message(MIETEK, f"/trial {OBCY}"))
    bot.handle_update(click(MIETEK, f"adm:ok:{OBCY}"))
    assert BotStore(bot.repo).get_user(OBCY).is_active is False
    assert "Nie rozumiem" in api.last_to(MIETEK)["text"]


def test_expired_subscription_blocks_the_bot_again(bot, api, clock):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 3"))
    clock.advance(days=3, minutes=1)

    bot.handle_update(message(MIETEK, "🔎 Filtry"))

    assert api.last_to(MIETEK)["text"].startswith("⛔ Twój abonament wygasł")
    assert "@admin_gunb" in api.last_to(MIETEK)["text"]


def test_lead_rounds_reach_only_paying_users(bot, api, repo):
    activate(bot, api, MIETEK)  # przycisk „✅ 30 dni”
    bot.handle_update(message(OBCY, "/start"))  # bez abonamentu
    repo.upsert(lead("A/1"))
    api.sent.clear()

    bot.deliver_reports("rano")
    bot.deliver_instant()

    assert api.to(MIETEK) and api.to(OBCY) == []


def test_expiry_is_announced_once_to_user_and_admin(bot, api, clock):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 3"))
    clock.advance(days=3, minutes=11)
    api.sent.clear()

    bot.run_due_jobs()
    clock.advance(minutes=11)
    bot.run_due_jobs()

    assert [m["text"][:30] for m in api.to(MIETEK)] == ["⛔ Twój abonament wygasł 02.10."]
    assert f"/aktywuj {MIETEK} 30" in api.to(ADMIN)[-1]["text"]


def test_admin_always_has_access_without_subscription(bot, api):
    bot.handle_update(message(ADMIN, "/start"))
    bot.handle_update(message(ADMIN, "🔎 Filtry"))
    assert "Twoje filtry" in api.last_to(ADMIN)["text"]


def test_users_list_shows_subscriptions(bot, api):
    activate(bot, api, MIETEK)
    bot.handle_update(message(OBCY, "/start"))
    bot.handle_update(message(ADMIN, "/start"))
    bot.handle_update(message(ADMIN, "/uzytkownicy"))
    text = api.last_to(ADMIN)["text"]
    assert "do 29.10.2026" in text and "nieaktywny" in text


def test_rejected_user_is_informed(bot, api):
    bot.handle_update(message(OBCY, "/start"))
    bot.handle_update(click(ADMIN, f"adm:no:{OBCY}"))
    assert BotStore(bot.repo).get_user(OBCY).status == "odrzucony"
    assert "⛔" in api.last_to(OBCY)["text"]


def test_open_access_lets_everyone_in_without_subscription(repo, api, clock):
    bot = make_bot(repo, api, clock, access="open")
    bot.handle_update(message(OBCY, "/start"))
    bot.handle_update(message(OBCY, "🔎 Filtry"))
    assert "Twoje filtry" in api.last_to(OBCY)["text"]


def test_unknown_text_shows_help_with_menu(bot, api):
    activate(bot, api)
    bot.handle_update(message(MIETEK, "dzień dobry"))
    assert api.last_to(MIETEK)["markup"]["keyboard"]


# --- Filtry przyciskami -------------------------------------------------------------------------

def test_filters_are_set_with_buttons_and_one_typed_place(bot, api):
    activate(bot, api)
    bot.repo.upsert(lead("WAW/1", **BIG_WARSAW))  # wpisana miejscowość musi być w monitorowanych danych
    bot.handle_update(message(MIETEK, "🔎 Filtry"))
    screen = api.last_to(MIETEK)
    assert "Twoje filtry" in screen["text"]

    bot.handle_update(click(MIETEK, callback_for(screen["markup"], "Rodzaj")))
    types = api.edits[-1]["markup"]
    bot.handle_update(click(MIETEK, callback_for(types, "Bloki")))
    assert "✅" in [t for t, _ in buttons(api.edits[-1]["markup"]) if "Bloki" in t][0]

    bot.handle_update(click(MIETEK, "f:vol"))
    bot.handle_update(click(MIETEK, callback_for(api.edits[-1]["markup"], "10 000")))

    bot.handle_update(click(MIETEK, "f:place"))
    bot.handle_update(click(MIETEK, callback_for(api.edits[-1]["markup"], "Wpisz")))
    assert "Napisz" in api.last_to(MIETEK)["text"]
    bot.handle_update(message(MIETEK, "Warszawa"))

    assert BotStore(bot.repo).get_user(MIETEK).filtry == UserFilters(
        miejsca=("Warszawa",), kategorie=("mieszkaniowa-wielorodzinna",), min_kubatura=10000)
    assert "Warszawa" in api.last_to(MIETEK)["text"]


def test_powiat_can_be_toggled_from_list(bot, api, repo):
    repo.upsert(lead("A/1"))
    activate(bot, api)
    bot.handle_update(click(MIETEK, "f:place"))
    places = buttons(api.edits[-1]["markup"])
    assert ("▫️ powiat poznański", "fp:3021") in places
    bot.handle_update(click(MIETEK, "fp:3021"))
    assert BotStore(repo).get_user(MIETEK).filtry.powiaty == ("3021",)


def test_clear_filters(bot, api):
    activate(bot, api)
    BotStore(bot.repo).set_filters(MIETEK, UserFilters(min_kubatura=5000))
    bot.handle_update(click(MIETEK, "f:clear"))
    assert BotStore(bot.repo).get_user(MIETEK).filtry.is_empty()


# --- Raport i przyciski pod leadem ------------------------------------------------------------------

def seed_leads(repo):
    repo.upsert(lead("WAW/1", **BIG_WARSAW))
    repo.upsert(lead("DOM/1"))
    repo.upsert(lead("HALA/1", kategoria="komercyjna", kubatura=5000.0, priorytet="hot", punkty=7,
                     nazwa_zamierzenia="Budowa hali magazynowej"))


def test_report_summarises_and_numbers_matching_leads(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    BotStore(repo).set_filters(MIETEK, UserFilters(miejsca=("Warszawa",), kategorie=("mieszkaniowa-wielorodzinna",),
                                                   min_kubatura=10000))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))

    report = api.last_to(MIETEK)
    assert "📊 <b>Raport 29.09</b>" in report["text"]
    assert "Znaleziono 3 nowe inwestycje. 1 spełnia Twoje filtry." in report["text"]
    assert "1 to 🔥 HOT (duża skala)." in report["text"]
    assert "Budowa zespołu dwóch budynków wielorodzinnych" in report["text"]
    nr = repo.get("WAW/1").nr
    assert buttons(report["markup"]) == [("1", f"o:{nr}")]

    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    again = api.last_to(MIETEK)["text"]  # nic nowego → pasujące z ostatnich dni (już widziane)
    assert "Nic nowego" in again
    assert "Budowa zespołu dwóch budynków wielorodzinnych" in again


def test_lead_card_has_action_buttons_and_save_updates_them(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    nr = repo.get("WAW/1").nr
    bot.handle_update(click(MIETEK, f"o:{nr}"))

    card = api.last_to(MIETEK)
    assert card["text"].startswith("🔥 <b>HOT</b>")
    labels = [t for t, _ in buttons(card["markup"])]
    assert labels[:2] == ["📍 Mapa", "🏛️ Geoportal"] or labels[0] == "📍 Mapa"
    assert {"⭐ Zapisz", "✅ Przejrzane", "🗑️ Ukryj", "⏰ Przypomnij", "📝 Notatka", "⋯ Więcej"} <= set(labels)
    bot.handle_update(click(MIETEK, f"mx:{nr}", message_id=card["message_id"]))  # obserwowanie – pod „⋯ Więcej”
    assert {"👀 Obserwuj inwestora", "📌 Obserwuj gminę"} <= {t for t, _ in buttons(api.edits[-1]["markup"])}

    bot.handle_update(click(MIETEK, f"s:{nr}", message_id=card["message_id"]))
    assert api.answers[-1].startswith("⭐ Zapisano")
    assert "⭐ Zapisany ✓" in [t for t, _ in buttons(api.edits[-1]["markup"])]

    bot.handle_update(message(MIETEK, "⭐ Zapisane"))
    saved = api.last_to(MIETEK)
    assert "Budowa zespołu dwóch budynków wielorodzinnych" in saved["text"]
    assert ("1", f"o:{nr}") in buttons(saved["markup"])


def test_hide_collapses_card_and_undo_restores_it(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    nr = repo.get("DOM/1").nr
    bot.handle_update(click(MIETEK, f"h:{nr}"))
    assert "Ukryte" in api.edits[-1]["text"]
    assert BotStore(repo).lead_state(MIETEK, "DOM/1") == "ukryty"
    bot.handle_update(click(MIETEK, f"u:{nr}"))
    assert BotStore(repo).lead_state(MIETEK, "DOM/1") is None
    assert "Budowa budynku mieszkalnego" in api.edits[-1]["text"]


def test_hot_only_toggle_filters_report(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    bot.handle_update(message(MIETEK, "🔥 Tylko HOT"))
    assert "tylko 🔥 HOT" in api.last_to(MIETEK)["text"]
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    report = api.last_to(MIETEK)["text"]
    assert "2 spełniają Twoje filtry" in report
    assert "Budowa budynku mieszkalnego jednorodzinnego" not in report


# --- Watchlista ---------------------------------------------------------------------------------

def test_watched_investor_triggers_instant_alert_even_in_morning_mode(bot, api, repo, clock):
    activate(bot, api)
    repo.upsert(lead("WAW/1", **BIG_WARSAW))
    bot.handle_update(click(MIETEK, f"wi:{repo.get('WAW/1').nr}"))
    assert api.answers[-1].startswith("👀 Obserwujesz")
    BotStore(repo).record_delivery(MIETEK, [repo.get("WAW/1")], "raport")

    clock.advance(hours=1)
    repo.upsert(lead("WAW/2", **{**BIG_WARSAW, "inwestor": "Napollo 4 Sp. z o.o.",
                                 "nazwa_zamierzenia": "Budowa budynku biurowego"}))
    bot.deliver_instant()

    alert = api.last_to(MIETEK)
    assert alert["text"].startswith("👀 <b>WATCHLISTA</b> · nowa inwestycja obserwowanego inwestora")
    clock.advance(hours=1)
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    assert "1 dotyczy obserwowanego inwestora" in api.last_to(MIETEK)["text"]


def test_watchlist_screen_allows_removal(bot, api, repo):
    activate(bot, api)
    repo.upsert(lead("DOM/1"))
    bot.handle_update(click(MIETEK, f"wg:{repo.get('DOM/1').nr}"))
    bot.handle_update(message(MIETEK, "👀 Obserwowane"))
    screen = api.last_to(MIETEK)
    assert "Kostrzyn" in screen["text"]
    bot.handle_update(click(MIETEK, callback_for(screen["markup"], "Kostrzyn")))
    assert BotStore(repo).watchlist(MIETEK) == []


# --- Tryby wysyłki i harmonogram -------------------------------------------------------------------

def test_instant_mode_sends_cards_and_digest_above_threshold(bot, api, repo):
    activate(bot, api)
    bot.handle_update(message(MIETEK, "⏰ Kiedy wysyłać"))
    bot.handle_update(click(MIETEK, "m:natychmiast"))
    assert BotStore(repo).get_user(MIETEK).tryb == "natychmiast"

    seed_leads(repo)
    api.sent.clear()
    bot.deliver_instant()
    assert len(api.to(MIETEK)) == 3
    assert all("⭐ Zapisz" in [t for t, _ in buttons(m["markup"])] for m in api.to(MIETEK))

    for n in range(12):
        repo.upsert(lead(f"NOWY/{n}"))
    api.sent.clear()
    bot.deliver_instant()
    (report,) = api.to(MIETEK)
    assert "📊 <b>Raport" in report["text"]


def test_morning_report_runs_once_per_day(bot, api, repo, clock):
    activate(bot, api)
    seed_leads(repo)
    clock.utc = datetime(2026, 9, 29, 5, 5, tzinfo=timezone.utc)  # 07:05 czasu lokalnego
    assert "raport_rano" in bot.run_due_jobs()
    assert "📊 <b>Raport" in api.last_to(MIETEK)["text"]
    sent = len(api.sent)
    assert "raport_rano" not in bot.run_due_jobs()
    assert len(api.sent) == sent


def test_blocked_user_is_marked_and_skipped(bot, api, repo):
    activate(bot, api)
    BotStore(repo).set_mode(MIETEK, "natychmiast")
    seed_leads(repo)
    api.blocked.add(MIETEK)
    bot.deliver_instant()
    assert BotStore(repo).get_user(MIETEK).status == "zablokowany"


def test_bot_commands_are_registered_on_setup(bot, api):
    bot.setup()
    assert [command for command, _ in api.commands] == ["nowe", "zapisane", "ustawienia", "konto", "pomoc"]


def test_long_report_is_shortened_to_fit_telegram_limit(bot, api, repo):
    activate(bot, api)
    for n in range(40):
        repo.upsert(lead(f"DLUGI/{n}", nazwa_zamierzenia="Budowa zespołu budynków " + "bardzo długi opis " * 10,
                         adres_opisowy="ul. " + "Bardzo Długa Nazwa Ulicy " * 5 + f"{n}, 00-001 Warszawa"))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    report = api.last_to(MIETEK)
    assert len(report["text"]) <= 4096
    assert "więcej" in report["text"]



# --- Zgłoszenie z testu na żywo: „ustawiłem filtry, a raport pusty” ----------------------------------

def test_filters_set_after_unfiltered_report_still_show_matching_leads(bot, api, repo):
    """Odtworzenie testu na żywo: najpierw raport bez filtrów, potem filtry „Warszawa + bloki + 10 000 m³”."""
    activate(bot, api)
    seed_leads(repo)
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))          # raport bez filtrów
    BotStore(repo).set_filters(MIETEK, UserFilters(miejsca=("Warszawa",), kategorie=("mieszkaniowa-wielorodzinna",),
                                                   min_kubatura=10000))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))

    text = api.last_to(MIETEK)["text"]
    assert "Budowa zespołu dwóch budynków wielorodzinnych" in text
    assert "Brak nowych" not in text


def test_only_leads_shown_in_report_count_as_seen(repo, api, clock):
    bot = make_bot(repo, api, clock, max_leads_in_report=2)
    activate(bot, api)
    seed_leads(repo)
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    first = api.last_to(MIETEK)
    assert len(buttons(first["markup"])) == 2
    assert "więcej" in first["text"]

    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    second = api.last_to(MIETEK)
    assert "Nic nowego" not in second["text"]
    assert len(buttons(second["markup"])) == 1  # trzeci lead, którego nie było na pierwszej liście


def test_filters_screen_offers_show_matching_button(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    bot.handle_update(message(MIETEK, "🔎 Filtry"))
    screen = api.last_to(MIETEK)
    bot.handle_update(click(MIETEK, callback_for(screen["markup"], "Pokaż pasujące")))
    assert "📊 <b>Raport" in api.last_to(MIETEK)["text"]


def test_nothing_matching_in_recent_days_says_so(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    BotStore(repo).set_filters(MIETEK, UserFilters(miejsca=("Gdańsk",)))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    text = api.last_to(MIETEK)["text"]
    assert "Nic nowego" in text
    assert "brak pasujących" in text


# --- Harmonogram: nieudane pobieranie GUNB jest ponawiane ------------------------------------------

def scheduled_fetcher(results):
    calls = []

    def fetcher():
        calls.append(1)
        return results.pop(0)

    return fetcher, calls


def test_failed_scheduled_fetch_is_retried_hourly_until_it_succeeds(repo, api, clock):
    fetcher, calls = scheduled_fetcher([False, False, True])
    bot = make_bot(repo, api, clock, fetch_times=("06:30",))
    bot.fetcher = fetcher
    clock.utc = datetime(2026, 9, 29, 4, 31, tzinfo=timezone.utc)  # 06:31 czasu lokalnego

    bot.run_due_jobs()
    assert len(calls) == 1  # 06:31 – GUNB nie odpowiada
    clock.advance(minutes=30)
    bot.run_due_jobs()
    assert len(calls) == 1  # 07:01 – za wcześnie na ponowienie
    clock.advance(minutes=31)
    bot.run_due_jobs()
    assert len(calls) == 2  # 07:32 – ponowienie, nadal awaria
    clock.advance(hours=1)
    bot.run_due_jobs()
    assert len(calls) == 3  # 08:32 – udane
    clock.advance(hours=3)
    bot.run_due_jobs()
    assert len(calls) == 3  # po sukcesie spokój do następnego terminu


def test_successful_fetch_is_not_repeated(repo, api, clock):
    fetcher, calls = scheduled_fetcher([None])  # dotychczasowe fetchery nic nie zwracają = sukces
    bot = make_bot(repo, api, clock, fetch_times=("06:30",))
    bot.fetcher = fetcher
    clock.utc = datetime(2026, 9, 29, 4, 31, tzinfo=timezone.utc)
    bot.run_due_jobs()
    clock.advance(hours=2)
    bot.run_due_jobs()
    assert len(calls) == 1


def test_fetch_crash_is_logged_and_retried_later(repo, api, clock, caplog):
    def crashing_fetcher():
        raise RuntimeError("database is locked")

    bot = make_bot(repo, api, clock, fetch_times=("06:30",))
    bot.fetcher = crashing_fetcher
    clock.utc = datetime(2026, 9, 29, 4, 31, tzinfo=timezone.utc)
    with caplog.at_level("ERROR"):
        bot.run_due_jobs()  # wyjątek nie zatrzymuje harmonogramu bota
    assert any(r.levelname == "ERROR" and r.exc_info for r in caplog.records)

    fetcher, calls = scheduled_fetcher([True])
    bot.fetcher = fetcher
    clock.advance(hours=1, minutes=1)
    bot.run_due_jobs()
    assert len(calls) == 1  # ponowienie po godzinie


# --- „📍 Blisko mnie”: pinezka bazy i promień --------------------------------------------------------

BASE = (53.7784, 20.4801)                    # baza firmy w Olsztynie
NEAREST = dict(lat=53.7800, lon=20.4900)     # < 1 km
NEAR = dict(lat=53.8285, lon=20.4867)        # Dywity, ok. 6 km
FAR = dict(lat=53.8649, lon=20.9569)         # Biskupiec, ok. 33 km


def pin(chat_id, lat, lon):
    return {"update_id": 3, "message": {"message_id": 3, "location": {"latitude": lat, "longitude": lon},
                                        "chat": {"id": chat_id, "type": "private"},
                                        "from": {"id": chat_id, "first_name": "Mietek", "username": None}}}


def seed_olsztyn(repo):
    repo.upsert(lead("DALEKO/1", nazwa_zamierzenia="Dom C daleko", **FAR))
    repo.upsert(lead("BLISKO/1", nazwa_zamierzenia="Dom B blisko", **NEAR))
    repo.upsert(lead("TUZ/1", nazwa_zamierzenia="Dom A tuż obok", **NEAREST))


def test_nearby_button_asks_for_a_location_pin(bot, api):
    activate(bot, api)
    bot.handle_update(message(MIETEK, "📍 Blisko mnie"))
    request = api.last_to(MIETEK)
    assert request["markup"]["keyboard"][0][0] == {"text": "📍 Wyślij moją lokalizację", "request_location": True}


def test_pin_sets_base_with_default_radius_and_brings_menu_back(bot, api, repo):
    activate(bot, api)
    seed_olsztyn(repo)
    BotStore(repo).set_filters(MIETEK, UserFilters(powiaty=("2862",)))

    bot.handle_update(pin(MIETEK, *BASE))

    filters = BotStore(repo).get_user(MIETEK).filtry
    assert filters.baza == BASE and filters.promien_km == 15
    assert filters.powiaty == ()  # promień zastępuje wybór powiatów
    confirmation, picker = api.to(MIETEK)[-2:]
    assert confirmation["markup"]["keyboard"]  # stałe menu wraca na dół ekranu
    assert ("✅ 15 km", "fr:15") in buttons(picker["markup"])


def test_report_lists_only_leads_within_radius_nearest_first_with_distance(bot, api, repo):
    activate(bot, api)
    seed_olsztyn(repo)
    bot.handle_update(pin(MIETEK, *BASE))

    bot.handle_update(message(MIETEK, "📊 Co nowego?"))

    text = api.last_to(MIETEK)["text"]
    assert "Dom C daleko" not in text
    assert text.index("Dom A tuż obok") < text.index("Dom B blisko")
    assert "📏 &lt;1 km" in text and "📏 6 km" in text


def test_hot_leads_stay_on_top_within_radius(bot, api, repo):
    activate(bot, api)
    repo.upsert(lead("TUZ/1", nazwa_zamierzenia="Dom tuż obok", **NEAREST))
    repo.upsert(lead("HOT/1", nazwa_zamierzenia="Blok HOT", priorytet="hot", punkty=9, **NEAR))
    bot.handle_update(pin(MIETEK, *BASE))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    text = api.last_to(MIETEK)["text"]
    assert text.index("Blok HOT") < text.index("Dom tuż obok")


def test_radius_is_changed_with_one_click(bot, api, repo):
    activate(bot, api)
    seed_olsztyn(repo)
    bot.handle_update(pin(MIETEK, *BASE))

    bot.handle_update(click(MIETEK, "fr:50"))

    assert BotStore(repo).get_user(MIETEK).filtry.promien_km == 50
    assert ("✅ 50 km", "fr:50") in buttons(api.edits[-1]["markup"])
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    assert "Dom C daleko" in api.last_to(MIETEK)["text"]


def test_radius_can_be_switched_off_and_base_is_remembered(bot, api, repo):
    activate(bot, api)
    bot.handle_update(pin(MIETEK, *BASE))
    bot.handle_update(click(MIETEK, "fr:0"))
    filters = BotStore(repo).get_user(MIETEK).filtry
    assert filters.promien_km is None and filters.baza == BASE


def test_choosing_a_powiat_switches_radius_off(bot, api, repo):
    activate(bot, api)
    bot.handle_update(pin(MIETEK, *BASE))
    bot.handle_update(click(MIETEK, "fp:2862"))
    filters = BotStore(repo).get_user(MIETEK).filtry
    assert filters.powiaty == ("2862",) and filters.promien_km is None


def test_lead_card_shows_distance_from_base(bot, api, repo):
    activate(bot, api)
    seed_olsztyn(repo)
    bot.handle_update(pin(MIETEK, *BASE))
    bot.handle_update(click(MIETEK, f"o:{repo.get('BLISKO/1').nr}"))
    assert "📏 6 km w linii prostej od Twojej bazy" in api.last_to(MIETEK)["text"]


def test_filters_screen_shows_radius(bot, api, repo):
    activate(bot, api)
    bot.handle_update(pin(MIETEK, *BASE))
    bot.handle_update(message(MIETEK, "🔎 Filtry"))
    assert "do 15 km od Twojej bazy" in api.last_to(MIETEK)["text"]


def test_radius_without_base_asks_for_pin_first(bot, api, repo):
    activate(bot, api)
    bot.handle_update(click(MIETEK, "fr:20"))
    assert BotStore(repo).get_user(MIETEK).filtry.promien_km is None
    assert api.last_to(MIETEK)["markup"]["keyboard"][0][0]["request_location"] is True


def test_cancel_on_location_request_brings_menu_back(bot, api, repo):
    activate(bot, api)
    bot.handle_update(message(MIETEK, "📍 Blisko mnie"))
    bot.handle_update(message(MIETEK, "↩️ Anuluj"))
    reply = api.last_to(MIETEK)
    assert [b["text"] for row in reply["markup"]["keyboard"] for b in row] == list(MENU_BUTTONS)
    assert BotStore(repo).get_user(MIETEK).filtry.baza is None


def test_clearing_filters_keeps_the_base(bot, api, repo):
    activate(bot, api)
    bot.handle_update(pin(MIETEK, *BASE))
    bot.handle_update(click(MIETEK, "f:clear"))
    filters = BotStore(repo).get_user(MIETEK).filtry
    assert filters.baza == BASE and filters.is_empty()


def test_pin_from_user_without_subscription_is_ignored(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(pin(MIETEK, *BASE))
    assert BotStore(repo).get_user(MIETEK).filtry.baza is None
    assert api.last_to(MIETEK)["text"] == GATE


def test_blisko_command_works_like_the_button(bot, api):
    activate(bot, api)
    bot.handle_update(message(MIETEK, "/blisko"))
    assert api.last_to(MIETEK)["markup"]["keyboard"][0][0]["request_location"] is True


# --- „⏰ Kiedy dzwonić”: przypomnienia według etapu budowy -------------------------------------------

def decided(months):
    day = (datetime(2026, 9, 29).date() - timedelta(days=round(months * 30.44))).isoformat()
    return dict(data_decyzji=day, data_aktualizacji=day)


def seed_stages(repo):
    repo.upsert(lead("DACH/1", nazwa_zamierzenia="Dom na etapie dachu", **decided(4.5)), )
    repo.upsert(lead("WCZESNIE/1", nazwa_zamierzenia="Dom świeżo po pozwoleniu", **decided(1)))
    repo.upsert(lead("POZNO/1", nazwa_zamierzenia="Dom już pod dachem", **decided(8)))


def reminders_to(api, chat_id):
    return [m for m in api.to(chat_id) if "Warto sprawdzić" in m["text"]]


def test_trade_is_chosen_from_filters_and_first_reminder_comes_at_once(bot, api, repo):
    activate(bot, api)
    seed_stages(repo)
    bot.handle_update(click(MIETEK, "f:trade"))
    assert ("🏠 Dach – 4–6 mies.", "fb:dach") in buttons(api.edits[-1]["markup"])

    bot.handle_update(click(MIETEK, "fb:dach"))

    assert BotStore(repo).get_user(MIETEK).branza == "dach"
    reminder = reminders_to(api, MIETEK)[-1]["text"]
    assert "Dom na etapie dachu" in reminder
    assert "świeżo po pozwoleniu" not in reminder and "już pod dachem" not in reminder
    assert f"decyzja {decided(4.5)['data_decyzji'][8:10]}." in reminder  # data pozwolenia przy budowie


def test_reminders_come_every_morning_without_repeats(repo, api, clock):
    bot = make_bot(repo, api, clock, morning_time="07:00")
    activate(bot, api)
    BotStore(repo).set_trade(MIETEK, "dach")
    repo.upsert(lead("DACH/1", nazwa_zamierzenia="Dom na etapie dachu", **decided(4.5)))
    repo.upsert(lead("JUTRO/1", nazwa_zamierzenia="Dom, który jutro wejdzie w dach", data_decyzji="2026-05-31",
                     data_aktualizacji="2026-05-31"))
    clock.utc = datetime(2026, 9, 29, 4, 59, tzinfo=timezone.utc)  # 06:59
    bot.run_due_jobs()
    assert reminders_to(api, MIETEK) == []

    clock.advance(minutes=2)  # 07:01
    bot.run_due_jobs()
    first = reminders_to(api, MIETEK)
    assert len(first) == 1 and "Dom na etapie dachu" in first[0]["text"]
    assert "jutro wejdzie" not in first[0]["text"]

    clock.advance(days=1)  # następny ranek: tylko budowa, która właśnie weszła w okno
    bot.run_due_jobs()
    second = reminders_to(api, MIETEK)[-1]["text"]
    assert "jutro wejdzie" in second and "Dom na etapie dachu" not in second


def test_reminders_respect_filters_and_hidden_leads(bot, api, repo):
    activate(bot, api)
    repo.upsert(lead("DACH/1", nazwa_zamierzenia="Dom blisko bazy", **decided(4.5), **NEAR))
    repo.upsert(lead("DALEKO/1", nazwa_zamierzenia="Dom daleko", **decided(4.5), **FAR))
    repo.upsert(lead("UKRYTY/1", nazwa_zamierzenia="Dom ukryty", **decided(4.5), **NEAREST))
    BotStore(repo).set_lead_state(MIETEK, "UKRYTY/1", "ukryty")
    bot.handle_update(pin(MIETEK, *BASE))

    bot.handle_update(click(MIETEK, "fb:dach"))

    reminder = reminders_to(api, MIETEK)[-1]["text"]
    assert "Dom blisko bazy" in reminder and "📏 6 km" in reminder
    assert "Dom daleko" not in reminder and "Dom ukryty" not in reminder


def test_foundation_trade_means_leads_at_once_without_reminders(bot, api, repo):
    activate(bot, api)
    seed_stages(repo)
    bot.handle_update(click(MIETEK, "fb:stan_surowy"))
    assert BotStore(repo).get_user(MIETEK).branza == "stan_surowy"
    assert reminders_to(api, MIETEK) == []
    assert "od razu" in api.last_to(MIETEK)["text"]


def test_nothing_at_stage_right_now_is_said_plainly(bot, api, repo):
    activate(bot, api)
    repo.upsert(lead("WCZESNIE/1", **decided(1)))
    bot.handle_update(click(MIETEK, "fb:elewacja"))
    assert "Na razie żadna" in api.last_to(MIETEK)["text"]


def test_reminders_can_be_switched_off(bot, api, repo):
    activate(bot, api)
    BotStore(repo).set_trade(MIETEK, "dach")
    bot.handle_update(click(MIETEK, "fb:none"))
    assert BotStore(repo).get_user(MIETEK).branza is None


def test_filters_screen_shows_trade_and_timing(bot, api, repo):
    activate(bot, api)
    BotStore(repo).set_trade(MIETEK, "okna")
    bot.handle_update(message(MIETEK, "🔎 Filtry"))
    assert "Okna i drzwi – przypomnę orientacyjnie 5–7 mies. po decyzji" in api.last_to(MIETEK)["text"]


def test_branza_command_opens_trade_picker(bot, api):
    activate(bot, api)
    bot.handle_update(message(MIETEK, "/branza"))
    assert ("🏠 Dach – 4–6 mies.", "fb:dach") in buttons(api.last_to(MIETEK)["markup"])


def test_reminded_lead_still_counts_as_new_in_normal_report(bot, api, repo):
    """Przypomnienie o etapie nie „zjada” leada z raportu nowości (osobna rewizja doręczenia)."""
    activate(bot, api)
    repo.upsert(lead("DACH/1", nazwa_zamierzenia="Dom na etapie dachu", **decided(4.5)))
    bot.handle_update(click(MIETEK, "fb:dach"))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    assert "Dom na etapie dachu" in api.last_to(MIETEK)["text"]


# --- P0.3: zapisanie, przejrzenie i ukrycie to niezależne informacje ---------------------------------

def flags(repo, id_sprawy):
    f = BotStore(repo).lead_flags(MIETEK, id_sprawy)
    return f.saved, f.reviewed, f.hidden


def test_reviewing_a_saved_investment_keeps_it_saved(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    nr = repo.get("WAW/1").nr
    bot.handle_update(click(MIETEK, f"s1:{nr}"))
    bot.handle_update(click(MIETEK, f"r1:{nr}"))

    assert flags(repo, "WAW/1") == (True, True, False)
    bot.handle_update(message(MIETEK, "⭐ Zapisane"))
    assert "Budowa zespołu dwóch budynków wielorodzinnych" in api.last_to(MIETEK)["text"]


def test_old_buttons_reviewed_after_saved_no_longer_unsave(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    nr = repo.get("WAW/1").nr
    bot.handle_update(click(MIETEK, f"s:{nr}"))  # przyciski z wiadomości sprzed aktualizacji
    bot.handle_update(click(MIETEK, f"r:{nr}"))
    assert flags(repo, "WAW/1") == (True, True, False)


def test_saving_can_be_switched_off(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    nr = repo.get("WAW/1").nr
    bot.handle_update(click(MIETEK, f"s1:{nr}"))
    bot.handle_update(click(MIETEK, f"s0:{nr}"))
    assert flags(repo, "WAW/1") == (False, False, False)
    bot.handle_update(message(MIETEK, "⭐ Zapisane"))
    assert "Nie masz jeszcze zapisanych" in api.last_to(MIETEK)["text"]


def test_hiding_and_restoring_keeps_saved_and_reviewed(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    nr = repo.get("WAW/1").nr
    for data in (f"s1:{nr}", f"r1:{nr}", f"h:{nr}"):
        bot.handle_update(click(MIETEK, data))
    assert flags(repo, "WAW/1") == (True, True, True)
    bot.handle_update(message(MIETEK, "⭐ Zapisane"))
    assert "Budowa zespołu dwóch budynków wielorodzinnych" not in api.last_to(MIETEK)["text"]  # ukryte znika

    bot.handle_update(click(MIETEK, f"u:{nr}"))

    assert flags(repo, "WAW/1") == (True, True, False)
    bot.handle_update(message(MIETEK, "⭐ Zapisane"))
    assert "Budowa zespołu dwóch budynków wielorodzinnych" in api.last_to(MIETEK)["text"]


def test_repeated_clicks_do_not_flip_the_state(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    nr = repo.get("WAW/1").nr
    for _ in range(3):
        bot.handle_update(click(MIETEK, f"s1:{nr}"))
        bot.handle_update(click(MIETEK, f"r1:{nr}"))
        bot.handle_update(click(MIETEK, f"u:{nr}"))
    assert flags(repo, "WAW/1") == (True, True, False)


def test_card_buttons_show_both_marks(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    nr = repo.get("WAW/1").nr
    bot.handle_update(click(MIETEK, f"s1:{nr}"))
    bot.handle_update(click(MIETEK, f"r1:{nr}"))
    labels = dict(buttons(api.edits[-1]["markup"]))
    assert labels["⭐ Zapisany ✓"] == f"s0:{nr}" and labels["✅ Przejrzany ✓"] == f"r0:{nr}"


def test_hidden_investment_leaves_the_report(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    bot.handle_update(click(MIETEK, f"h:{repo.get('DOM/1').nr}"))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    assert "Budowa budynku mieszkalnego jednorodzinnego" not in api.last_to(MIETEK)["text"]


# --- P0.4: wyniki zgodne z filtrami, pełne przeglądanie historii --------------------------------------

def seed_far_and_one_match(repo):
    """1000 nowszych inwestycji spoza obszaru i jedna starsza, pasująca (Olsztyn)."""
    with repo.transaction():
        for i in range(1000):
            day = f"2026-09-{20 + i % 8:02d}"
            repo.upsert(lead(f"DALEKO/{i}", data_aktualizacji=day, powiat_teryt="3021", gmina_teryt="3021085",
                             gmina="Kostrzyn", miejscowosc="Wróblewo", adres_opisowy="Wróblewo"))
        repo.upsert(lead("OLSZTYN/1", nazwa_zamierzenia="Dom w Olsztynie", data_aktualizacji="2026-09-05",
                         powiat_teryt="2862", gmina_teryt="2862011", gmina="Olsztyn (miasto)", miejscowosc="Olsztyn",
                         adres_opisowy="Olsztyn", powiat="powiat Olsztyn"))


def test_match_older_than_a_thousand_others_is_found(bot, api, repo):
    activate(bot, api)
    seed_far_and_one_match(repo)
    # pasująca była już kiedyś wysłana – znaleźć ją można tylko w przeglądzie historii
    BotStore(repo).record_delivery(MIETEK, [repo.get("OLSZTYN/1")], "raport")
    BotStore(repo).set_filters(MIETEK, UserFilters(powiaty=("2862",)))

    bot.handle_update(message(MIETEK, "📊 Co nowego?"))

    text = api.last_to(MIETEK)["text"]
    assert "Dom w Olsztynie" in text
    assert "(1)" in text  # liczba odpowiada temu, co da się zobaczyć


def history_pages(api):
    return [m for m in api.sent + api.edits if m.get("text") and "Pasujące" in m["text"]]


def seed_matches(repo, count):
    with repo.transaction():
        for i in range(count):
            repo.upsert(lead(f"P/{i:02d}", nazwa_zamierzenia=f"Inwestycja numer {i:02d}",
                             data_aktualizacji=f"2026-09-{1 + i % 28:02d}"))


def test_history_is_browsed_with_stable_next_and_back(bot, api, repo):
    activate(bot, api)
    seed_matches(repo, 25)
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))  # 25 nowych → raport zużywa pokazane
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))  # nic nowego → przegląd historii
    first = api.last_to(MIETEK)
    assert "(25)" in first["text"]
    assert ("Dalej ▶️", "hp:1") in buttons(first["markup"])
    shown = set()
    for page in range(3):
        bot.handle_update(click(MIETEK, f"hp:{page}", message_id=first["message_id"]))
        page_text = api.edits[-1]["text"]
        shown |= {line for line in page_text.splitlines() if "Inwestycja numer" in line}
    assert len(shown) == 25  # każda pasująca dokładnie raz na którejś stronie
    assert ("◀️ Wstecz", "hp:1") in buttons(api.edits[-1]["markup"])
    assert "Dalej ▶️" not in [t for t, _ in buttons(api.edits[-1]["markup"])]


def test_browsing_history_does_not_consume_new_notifications(bot, api, repo):
    activate(bot, api)
    seed_matches(repo, 3)
    bot.handle_update(click(MIETEK, "hp:0"))  # przegląd historii zanim przyszedł raport
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    assert "Znaleziono 3 nowe inwestycje" in api.last_to(MIETEK)["text"]


def test_no_results_means_really_nothing_matches(bot, api, repo):
    activate(bot, api)
    seed_matches(repo, 3)
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    BotStore(repo).set_filters(MIETEK, UserFilters(miejsca=("Gdańsk",)))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    text = api.last_to(MIETEK)["text"]
    assert "brak pasujących" in text.lower()
    assert "Gdańsk" in text  # pokazane aktywne filtry, bez kasowania
    assert BotStore(repo).get_user(MIETEK).filtry.miejsca == ("Gdańsk",)
