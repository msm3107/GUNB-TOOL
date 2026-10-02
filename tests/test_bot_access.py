"""Dostęp (P0.5): 7-dniowy test, abonament nadawany przez admina, uprawnienia sprawdzane centralnie."""

from datetime import datetime, timedelta, timezone

import pytest

from gunb_tool.bot_store import BotStore
from gunb_tool.storage import LeadRepository
from gunb_tool.telegram_api import TelegramApiError
from tests.bot_helpers import ADMIN, MIETEK, OBCY, activate, buttons, click, configured, lead, make_bot, message

START = "ts"  # przycisk „▶️ Zacznij 7-dniowy test”
TRIAL_END = "06.10.2026, 07:00"  # start 29.09 07:00 czasu polskiego + 7 × 24 h


@pytest.fixture
def bot(repo, api, clock):
    return make_bot(repo, api, clock)


def offer_trial(bot, api, chat_id=MIETEK):
    """Nowa osoba pisze /start i ma za sobą pierwsze kroki; admin pozwala na test przyciskiem z karty.

    Same pierwsze kroki (branża, obszar) – tests/test_bot_onboarding.py.
    """
    bot.handle_update(message(chat_id, "/start"))
    configured(bot, chat_id)
    bot.handle_update(click(ADMIN, f"adm:trial:{chat_id}"))


def start_trial(bot, api, chat_id=MIETEK):
    offer_trial(bot, api, chat_id)
    bot.handle_update(click(chat_id, START))
    api.sent.clear()
    api.answers.clear()
    api.edits.clear()


def user(repo, chat_id=MIETEK):
    return BotStore(repo).get_user(chat_id)


# --- Start testu ------------------------------------------------------------------------------------

def test_admin_allows_the_trial_but_it_starts_only_when_the_user_clicks_start(bot, api, repo, clock):
    offer_trial(bot, api)

    offer = api.last_to(MIETEK)
    assert "7 dni" in offer["text"] and (("▶️ Zacznij 7-dniowy test", START) in buttons(offer["markup"]))
    assert "7 dni" in [e for e in api.edits if e["chat_id"] == ADMIN][-1]["text"]  # karta admina potwierdza
    assert user(repo).test_start is None  # samo pozwolenie niczego nie odlicza
    clock.advance(days=2)  # klient zaczyna, kiedy chce

    bot.handle_update(click(MIETEK, START))

    started = user(repo)
    assert started.test_start == "2026-10-01T05:00:00+00:00"
    assert started.subscription_ends == "2026-10-08T05:00:00+00:00"  # dokładnie 7 × 24 h
    assert "08.10.2026, 07:00" in api.to(MIETEK)[-2]["text"]  # dokładna data i godzina końca (potem przegląd)


def test_trial_waiting_for_start_allows_setup_but_not_data(bot, api, repo):
    offer_trial(bot, api)
    repo.upsert(lead("A/1"))

    bot.handle_update(message(MIETEK, "🔎 Filtry"))
    assert "Twoje filtry" in api.last_to(MIETEK)["text"]  # ustawienia przed startem testu – tak

    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    reply = api.last_to(MIETEK)
    assert "jeszcze się nie zaczął" in reply["text"] and (("▶️ Zacznij 7-dniowy test", START) in buttons(reply["markup"]))


def test_trial_lasts_exactly_seven_days_and_automatic_rounds_stop_after_it(bot, api, repo, clock):
    start_trial(bot, api)
    clock.advance(days=7, seconds=-1)
    repo.upsert(lead("A/1"))
    assert bot.deliver_reports("rano") == 1  # ostatnia sekunda testu

    clock.advance(seconds=1)
    repo.upsert(lead("B/1"))
    api.sent.clear()
    bot.deliver_reports("rano")
    bot.deliver_instant()
    assert api.to(MIETEK) == []


def test_repeated_start_clicks_do_not_move_the_end(bot, api, repo, clock):
    start_trial(bot, api)
    clock.advance(hours=5)
    bot.handle_update(click(MIETEK, START))
    assert user(repo).subscription_ends == "2026-10-06T05:00:00+00:00"
    assert TRIAL_END in api.answers[-1]


@pytest.mark.parametrize("action", ["start", "restart", "filters", "unblock"])
def test_trial_is_not_renewed_by_start_restart_filters_or_unblocking(bot, api, repo, clock, action):
    start_trial(bot, api)
    clock.advance(days=3)
    if action == "start":
        bot.handle_update(message(MIETEK, "/start"))
    elif action == "restart":
        make_bot(repo, api, clock).handle_update(message(MIETEK, "/start"))
    elif action == "filters":
        bot.handle_update(click(MIETEK, "ft:0"))
    else:
        BotStore(repo).set_status(MIETEK, "zablokowany")
        bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    assert user(repo).subscription_ends == "2026-10-06T05:00:00+00:00"
    assert user(repo).test_start == "2026-09-29T05:00:00+00:00"


def test_only_one_trial_per_user(bot, api, repo, clock):
    start_trial(bot, api)
    clock.advance(days=8)

    bot.handle_update(message(ADMIN, f"/trial {MIETEK}"))
    assert "wykorzystał już" in api.last_to(ADMIN)["text"]
    bot.handle_update(click(MIETEK, START))  # stary przycisk startu

    assert "wykorzystany" in api.answers[-1]
    assert user(repo).subscription_ends == "2026-10-06T05:00:00+00:00"


# --- Po końcu testu -------------------------------------------------------------------------------------

def test_after_expiry_saved_items_and_settings_stay_but_old_buttons_show_nothing_new(bot, api, repo, clock):
    start_trial(bot, api)
    repo.upsert(lead("A/1"))
    nr = repo.get("A/1").nr
    bot.handle_update(click(MIETEK, f"s1:{nr}"))
    bot.handle_update(click(MIETEK, "ft:0"))  # filtr: domy jednorodzinne
    settings_before = user(repo).filtry
    clock.advance(days=7, minutes=1)
    api.sent.clear()
    api.edits.clear()

    for data in ("hp:1", "f:go", f"s0:{nr}"):
        bot.handle_update(click(MIETEK, data))
        assert "⛔" in api.answers[-1]
    bot.handle_update(click(MIETEK, f"o:{nr}"))  # zapisana – do wglądu w trybie archiwum, bez akcji
    assert api.last_to(MIETEK)["text"].startswith("🗄️ <b>Twoja zapisana praca</b>")
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))

    assert api.edits == [] and all("📊 <b>Raport" not in m["text"] for m in api.to(MIETEK))
    assert api.last_to(MIETEK)["text"].startswith("⛔ Twój darmowy test skończył się")
    assert BotStore(repo).lead_flags(MIETEK, "A/1").saved  # zapisane zostają
    assert user(repo).filtry == settings_before  # ustawienia też


def test_account_and_help_work_after_expiry(bot, api, repo, clock):
    start_trial(bot, api)
    clock.advance(days=8)

    bot.handle_update(message(MIETEK, "/konto"))
    account = api.last_to(MIETEK)
    assert "skończył się" in account["text"] and TRIAL_END in account["text"]
    assert ("💬 Zapytaj o ofertę", "zm:q") in buttons(account["markup"])  # jak wrócić – bez szukania kontaktu

    bot.handle_update(message(MIETEK, "/pomoc"))
    assert "Jak to działa" in api.last_to(MIETEK)["text"]


def test_unblocking_the_bot_does_not_bring_back_expired_access(bot, api, repo, clock):
    start_trial(bot, api)
    clock.advance(days=8)
    BotStore(repo).set_status(MIETEK, "zablokowany")

    bot.handle_update(message(MIETEK, "📊 Co nowego?"))  # odblokował i pisze

    assert user(repo).status == "aktywny"
    assert api.last_to(MIETEK)["text"].startswith("⛔")
    assert not bot._has_access(user(repo))


def test_one_reminder_before_the_end_and_one_notice_after_it_even_across_restarts(bot, api, repo, clock):
    start_trial(bot, api)
    clock.utc = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)  # 10:00 dzień przed końcem
    bot.run_due_jobs()
    restarted = make_bot(repo, api, clock)
    restarted.run_due_jobs()
    clock.advance(hours=3)
    restarted.run_due_jobs()

    reminders = [m for m in api.to(MIETEK) if "kończy się" in m["text"]]
    assert len(reminders) == 1 and TRIAL_END in reminders[0]["text"]

    clock.utc = datetime(2026, 10, 6, 5, 11, tzinfo=timezone.utc)  # 07:11 – po końcu
    restarted.run_due_jobs()
    make_bot(repo, api, clock).run_due_jobs()
    clock.advance(hours=2)
    make_bot(repo, api, clock).run_due_jobs()

    notices = [m for m in api.to(MIETEK) if "skończył się" in m["text"]]
    assert len(notices) == 1
    assert sum(f"/aktywuj {MIETEK}" in m["text"] for m in api.to(ADMIN)) == 1  # admin wie, komu przedłużyć


def test_no_expiry_messages_at_night(bot, api, repo, clock):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 2026-10-01"))  # do 01.10 23:59
    api.sent.clear()
    clock.utc = datetime(2026, 10, 1, 22, 30, tzinfo=timezone.utc)  # 00:30 – już po terminie, noc
    bot.run_due_jobs()
    assert api.to(MIETEK) == []
    clock.utc = datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc)  # 07:00
    bot.run_due_jobs()
    assert [m["text"][:27] for m in api.to(MIETEK)] == ["⛔ Twój dostęp wygasł 01.10."]


# --- Admin: termin, przedłużenie, odebranie ---------------------------------------------------------------

def test_admin_grants_access_until_a_date(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 31.12.2026"))
    assert user(repo).subscription_ends == "2026-12-31T22:59:00+00:00"  # 23:59 czasu polskiego (zima)
    assert any("31.12.2026, 23:59" in m["text"] for m in api.to(MIETEK))


def test_admin_extends_from_the_current_end(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 30"))
    bot.handle_update(message(ADMIN, f"/przedluz {MIETEK} 30"))
    assert user(repo).subscription_ends == "2026-11-28T05:00:00+00:00"


def test_admin_revokes_access_at_once(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 30"))
    bot.handle_update(message(ADMIN, f"/odbierz {MIETEK}"))

    assert not bot._has_access(user(repo))
    assert "wyłączony" in api.last_to(MIETEK)["text"]
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    assert api.last_to(MIETEK)["text"].startswith("⛔")


def test_account_screen_says_access_was_turned_off(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 30"))
    bot.handle_update(message(ADMIN, f"/odbierz {MIETEK}"))

    bot.handle_update(message(MIETEK, "/konto"))

    account = api.last_to(MIETEK)["text"]
    assert "wyłączony" in account and "jeszcze nieaktywny" not in account


def test_admin_date_in_the_past_is_refused(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 2026-09-01"))
    assert "Użycie" in api.last_to(ADMIN)["text"]
    assert not bot._has_access(user(repo))


def test_admin_never_needs_a_trial(bot, api, repo, clock):
    activate(bot, api, ADMIN)
    clock.advance(days=400)
    bot.handle_update(message(ADMIN, "📊 Co nowego?"))
    assert not api.last_to(ADMIN)["text"].startswith("⛔")
    bot.run_due_jobs()
    assert all("kończy się" not in m["text"] and "wygasł" not in m["text"] for m in api.to(ADMIN))


# --- Kto klika ----------------------------------------------------------------------------------------------

def test_button_is_authorised_by_the_person_who_clicks(bot, api, repo):
    activate(bot, api, MIETEK)
    bot.handle_update(message(OBCY, "/start"))  # bez dostępu
    repo.upsert(lead("A/1"))
    nr = repo.get("A/1").nr
    stolen = click(OBCY, f"s1:{nr}")
    stolen["callback_query"]["message"]["chat"]["id"] = MIETEK  # przycisk z cudzej wiadomości

    bot.handle_update(stolen)

    assert "⛔" in api.answers[-1]
    assert not BotStore(repo).lead_flags(MIETEK, "A/1").saved
    assert not BotStore(repo).lead_flags(OBCY, "A/1").saved


# --- Dotychczasowi użytkownicy (migracja) -------------------------------------------------------------------

def test_users_approved_before_subscriptions_keep_access_until_admin_switches_them(tmp_path, api, clock):
    from tests.test_storage import legacy_database

    path = tmp_path / "v5.sqlite"
    legacy = legacy_database(path, 5)
    for chat_id, status in ((MIETEK, "aktywny"), (OBCY, "zablokowany"), (4004, "oczekuje"), (5005, "odrzucony")):
        legacy.execute("INSERT INTO bot_users (chat_id, status, nowe_od, utworzono, zmieniono)"
                       " VALUES (?, ?, '2026-09-01T00:00:00+00:00', 'x', 'x')", (chat_id, status))
    legacy.commit()
    legacy.close()

    repo = LeadRepository(path, now=clock.now_utc)
    bot = make_bot(repo, api, clock)
    assert [bot._has_access(user(repo, c)) for c in (MIETEK, OBCY, 4004, 5005)] == [True, True, False, False]
    repo.upsert(lead("A/1"))
    assert bot.deliver_reports("rano") == 1  # dotychczasowy użytkownik dalej dostaje raporty

    bot.handle_update(message(ADMIN, "/nowymodel wszyscy 30"))

    switched = user(repo)
    assert switched.subscription_ends == "2026-10-29T05:00:00+00:00" and not switched.bez_limitu
    assert "29.10.2026" in api.last_to(MIETEK)["text"]
    repo.close()


def test_subscriptions_from_the_previous_version_are_kept(tmp_path, api, clock):
    from tests.test_storage import legacy_database

    path = tmp_path / "v6.sqlite"
    legacy = legacy_database(path, 6)
    legacy.execute("INSERT INTO bot_users (chat_id, status, nowe_od, utworzono, zmieniono, is_active, subscription_ends)"
                   " VALUES (?, 'aktywny', 'x', 'x', 'x', 1, '2026-10-10T05:00:00+00:00')", (MIETEK,))
    legacy.execute("INSERT INTO bot_users (chat_id, status, nowe_od, utworzono, zmieniono)"
                   " VALUES (?, 'aktywny', 'x', 'x', 'x')", (OBCY,))  # zarejestrowany po wprowadzeniu abonamentów
    legacy.commit()
    legacy.close()

    with LeadRepository(path, now=clock.now_utc) as repo:
        bot = make_bot(repo, api, clock)
        assert bot._has_access(user(repo, MIETEK)) and not bot._has_access(user(repo, OBCY))


def test_recipients_query_matches_the_access_rule(bot, api, repo, clock):
    store = BotStore(repo)
    for chat_id in range(10, 19):
        store.register(chat_id, None, None, status="aktywny", backlog_days=7)
    store.set_access(18, "2026-10-01T00:00:00+00:00")
    store.set_paused(18, True)  # dostęp ma, ale wstrzymał powiadomienia
    store.set_access(11, "2026-10-01T00:00:00+00:00")  # ważny
    store.set_access(12, "2026-09-01T00:00:00+00:00")  # wygasły
    store.allow_trial(13)  # test dozwolony, nierozpoczęty
    store.allow_trial(14)
    store.start_trial(14, clock.utc, clock.utc + timedelta(days=7))
    repo.connection.execute("UPDATE bot_users SET dostep_bez_limitu = 1 WHERE chat_id = 15")
    store.set_access(16, "2026-10-01T00:00:00+00:00")
    store.set_status(16, "zablokowany")
    store.set_access(17, "2026-10-01T00:00:00+00:00")
    store.revoke_access(17)

    everyone = [u for status in ("aktywny", "zablokowany") for u in store.users(status)]
    by_rule = {u.chat_id for u in everyone if bot._receives_automatic(u)}
    by_query = {u.chat_id for u in bot._subscribers()}
    assert by_query == by_rule == {11, 14, 15}
    assert bot._has_access(store.get_user(18))  # pauza nie odbiera dostępu


def test_access_end_notice_waits_for_the_morning_instead_of_being_lost(bot, api, repo, clock):
    clock.utc = datetime(2026, 9, 29, 19, 50, tzinfo=timezone.utc)  # 21:50
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 3"))  # koniec 02.10 o 21:50
    api.sent.clear()
    clock.utc = datetime(2026, 10, 2, 19, 58, tzinfo=timezone.utc)  # 21:58 – po końcu, tuż przed ciszą nocną
    api.fail(MIETEK, TelegramApiError("sendMessage", 502, "Bad Gateway"))
    bot.run_due_jobs()  # pierwsza próba nieudana
    clock.utc = datetime(2026, 10, 2, 20, 14, tzinfo=timezone.utc)  # 22:14 – ponowienie wypada w ciszy nocnej
    bot.run_due_jobs()
    clock.utc = datetime(2026, 10, 3, 4, 1, tzinfo=timezone.utc)  # 06:01 następnego dnia
    for _ in range(2):
        bot.run_due_jobs()
        clock.advance(minutes=20)

    notices = [m for m in api.to(MIETEK) if m["text"].startswith("⛔ Twój dostęp wygasł")]
    assert len(notices) == 1
