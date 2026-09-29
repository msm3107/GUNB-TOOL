"""P1.3: „⏰ Przypomnij” i prywatna notatka pod inwestycją; P1.4: pauza, zdarzenia i raport pilotażu."""

from datetime import datetime, timedelta, timezone

import pytest

from gunb_tool.bot_store import BotStore
from tests.bot_helpers import ADMIN, MIETEK, OBCY, activate, buttons, click, lead, make_bot, message

TODAY = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)  # 07:00 czasu polskiego


def morning(days: int, hour: int = 7, minute: int = 1) -> datetime:
    """Dzień za ``days`` dni o podanej godzinie czasu polskiego (październik 2026 – do 25.10 UTC+2)."""
    local = datetime(2026, 9, 29, hour, minute) + timedelta(days=days)
    offset = 2 if local < datetime(2026, 10, 25, 3) else 1
    return (local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)


@pytest.fixture
def bot(repo, api, clock):
    bot = make_bot(repo, api, clock)
    activate(bot, api, MIETEK)
    repo.upsert(lead("A/1", nazwa_zamierzenia="Dom pana Kowalskiego"))
    return bot


def nr(repo, id_sprawy="A/1"):
    return repo.get(id_sprawy).nr


def reminders(api, chat_id=MIETEK):
    return [m for m in api.to(chat_id) if m["text"].startswith("⏰ <b>Przypomnienie")]


def settle(bot, api):
    """Pierwszy cykl dnia (raport poranny itd.) – żeby dalej liczyły się tylko przypomnienia."""
    bot.run_due_jobs()
    api.sent.clear()


# --- P1.3: ⏰ Przypomnij --------------------------------------------------------------------------------

def test_card_has_remind_and_note_buttons(bot, api, repo):
    bot.handle_update(click(MIETEK, f"o:{nr(repo)}"))
    labels = [t for t, _ in buttons(api.last_to(MIETEK)["markup"])]
    assert "⏰ Przypomnij" in labels and "📝 Notatka" in labels and "⋯ Więcej" in labels


def test_reminder_comes_on_the_chosen_day_in_the_morning(bot, api, repo, clock):
    bot.handle_update(click(MIETEK, f"pr:{nr(repo)}"))
    assert {"7 dni", "14 dni", "30 dni"} <= {t for t, _ in buttons(api.edits[-1]["markup"])}

    bot.handle_update(click(MIETEK, f"pr:{nr(repo)}:7"))
    assert "06.10" in api.answers[-1]
    settle(bot, api)

    clock.utc = morning(7, hour=6, minute=59)
    bot.run_due_jobs()
    assert reminders(api) == []
    clock.utc = morning(7)
    bot.run_due_jobs()
    (reminder,) = reminders(api)
    assert "Dom pana Kowalskiego" in reminder["text"]
    clock.advance(hours=2)
    bot.run_due_jobs()
    assert len(reminders(api)) == 1


def test_choosing_again_moves_the_single_reminder(bot, api, repo, clock):
    bot.handle_update(click(MIETEK, f"pr:{nr(repo)}:7"))
    bot.handle_update(click(MIETEK, f"pr:{nr(repo)}:14"))
    settle(bot, api)

    clock.utc = morning(7)
    bot.run_due_jobs()
    assert reminders(api) == []
    clock.utc = morning(14)
    bot.run_due_jobs()
    assert len(reminders(api)) == 1
    assert repo.connection.execute("SELECT COUNT(*) FROM przypomnienia").fetchone()[0] == 0


def test_reminder_can_be_cancelled(bot, api, repo, clock):
    bot.handle_update(click(MIETEK, f"pr:{nr(repo)}:7"))
    bot.handle_update(click(MIETEK, f"pr:{nr(repo)}:0"))
    settle(bot, api)
    clock.utc = morning(7)
    bot.run_due_jobs()
    assert reminders(api) == []


def test_reminder_survives_a_restart(bot, api, repo, clock):
    bot.handle_update(click(MIETEK, f"pr:{nr(repo)}:14"))
    restarted = make_bot(repo, api, clock)
    settle(restarted, api)
    clock.utc = morning(14)
    restarted.run_due_jobs()
    make_bot(repo, api, clock).run_due_jobs()
    assert len(reminders(api)) == 1


def test_reminders_never_come_at_night(bot, api, repo, clock):
    bot.handle_update(click(MIETEK, f"pr:{nr(repo)}:7"))
    settle(bot, api)
    clock.utc = morning(7, hour=23, minute=10) - timedelta(days=1)  # dzień wcześniej, 23:10
    bot.run_due_jobs()
    clock.utc = morning(7, hour=23, minute=10)  # bot wrócił po awarii dopiero w nocy
    bot.run_due_jobs()
    assert reminders(api) == []
    clock.utc = morning(8)
    bot.run_due_jobs()
    assert len(reminders(api)) == 1


def test_reminders_wait_without_access_and_come_together_after_it(bot, api, repo, clock):
    repo.upsert(lead("B/1", nazwa_zamierzenia="Hala"))
    bot.handle_update(click(MIETEK, f"pr:{nr(repo)}:7"))
    bot.handle_update(click(MIETEK, f"pr:{nr(repo, 'B/1')}:7"))
    settle(bot, api)
    bot.handle_update(message(ADMIN, f"/odbierz {MIETEK}"))
    clock.utc = morning(7)
    bot.run_due_jobs()
    assert reminders(api) == []

    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 30"))
    clock.advance(minutes=11)
    bot.run_due_jobs()
    (together,) = reminders(api)  # zaległe – jedną wiadomością, bez zalewu
    assert "Dom pana Kowalskiego" in together["text"] and "Hala" in together["text"]


# --- P1.3: notatka ----------------------------------------------------------------------------------------

def test_note_is_private_escaped_and_limited(bot, api, repo):
    activate(bot, api, OBCY)
    bot.handle_update(click(MIETEK, f"nt:{nr(repo)}"))
    assert "300" in api.last_to(MIETEK)["text"]

    bot.handle_update(message(MIETEK, "x" * 301))
    assert "za długa" in api.last_to(MIETEK)["text"].lower()
    bot.handle_update(message(MIETEK, "<b>Kowalski</b> & syn – dzwonić po 16"))

    card = api.last_to(MIETEK)
    assert "📝 Twoja notatka: &lt;b&gt;Kowalski&lt;/b&gt; &amp; syn – dzwonić po 16" in card["text"]
    bot.handle_update(click(OBCY, f"o:{nr(repo)}"))
    assert "Kowalski</b>" not in api.last_to(OBCY)["text"] and "notatka" not in api.last_to(OBCY)["text"]


def test_note_can_be_changed_and_deleted(bot, api, repo):
    bot.handle_update(click(MIETEK, f"nt:{nr(repo)}"))
    bot.handle_update(message(MIETEK, "pierwsza"))
    bot.handle_update(click(MIETEK, f"nt:{nr(repo)}:e"))
    bot.handle_update(message(MIETEK, "druga"))
    assert "📝 Twoja notatka: druga" in api.last_to(MIETEK)["text"]

    bot.handle_update(click(MIETEK, f"nt:{nr(repo)}:d"))
    assert BotStore(repo).note(MIETEK, "A/1") is None
    assert "🗑️" in api.answers[-1]


# --- P1.4: pauza ------------------------------------------------------------------------------------------

def test_pause_stops_every_automatic_message_but_not_browsing(bot, api, repo, clock):
    store = BotStore(repo)
    store.set_mode(MIETEK, "natychmiast")
    store.set_trade(MIETEK, "dach")
    store.add_watch(MIETEK, "gmina", "3021085", "Kostrzyn")
    bot.handle_update(click(MIETEK, f"pr:{nr(repo)}:7"))
    bot.handle_update(message(MIETEK, "⚙️ Ustawienia"))
    bot.handle_update(click(MIETEK, "st:p"))
    assert store.get_user(MIETEK).wstrzymane
    api.sent.clear()

    repo.upsert(lead("C/1", nazwa_zamierzenia="Nowy dom", data_decyzji="2026-05-15"))
    for day in (0, 7):
        clock.utc = morning(day)
        bot.run_due_jobs()
    assert api.to(MIETEK) == []  # ani raportu, ani „od razu”, ani obserwowanych, ani przypomnień

    bot.handle_update(message(MIETEK, "📊 Inwestycje"))
    assert "Nowy dom" in api.last_to(MIETEK)["text"]  # przeglądanie działa dalej


def test_resume_brings_waiting_messages_without_flooding(bot, api, repo, clock):
    store = BotStore(repo)
    store.add_watch(MIETEK, "gmina", "3021085", "Kostrzyn")
    store.set_paused(MIETEK, True)
    for n in range(6):
        repo.upsert(lead(f"W/{n}", nazwa_zamierzenia=f"Budowa {n}"))
    bot.run_due_jobs()
    api.sent.clear()

    store.set_paused(MIETEK, False)
    clock.advance(minutes=11)
    bot.run_due_jobs()

    assert len(api.to(MIETEK)) == 1  # sześć alertów obserwowanej gminy – w jednej wiadomości
    assert "Budowa 5" in api.to(MIETEK)[0]["text"]


def test_pause_does_not_touch_access(bot, api, repo):
    ends = BotStore(repo).get_user(MIETEK).subscription_ends
    bot.handle_update(message(MIETEK, "⚙️ Ustawienia"))
    bot.handle_update(click(MIETEK, "st:p"))
    assert BotStore(repo).get_user(MIETEK).subscription_ends == ends
    bot.handle_update(message(MIETEK, "⚙️ Ustawienia"))
    assert "wstrzymane" in api.last_to(MIETEK)["text"]
    assert ("▶️ Wznów powiadomienia", "st:p") in buttons(api.last_to(MIETEK)["markup"])


# --- P1.4: zdarzenia i raport pilotażu ------------------------------------------------------------------------

def test_feedback_lives_under_more_actions_and_is_recorded(bot, api, repo):
    bot.handle_update(click(MIETEK, f"mx:{nr(repo)}"))
    more = [t for t, _ in buttons(api.edits[-1]["markup"])]
    assert {"👍 Przydatne", "👎 Nieprzydatne", "◀️ Wróć"} <= set(more)
    assert any("Obserwuj" in t for t in more)

    bot.handle_update(click(MIETEK, f"fu:{nr(repo)}:1"))
    assert "Dzięki" in api.answers[-1]
    assert BotStore(repo).event_counts(TODAY - timedelta(days=1))["przydatne"] == (1, 1)


def test_pilot_report_counts_unique_people_and_investments(bot, api, repo, clock):
    activate(bot, api, OBCY)
    repo.upsert(lead("B/1"))
    bot.deliver_reports("rano")  # obie osoby dostają A/1 i B/1
    for _ in range(2):
        bot.handle_update(click(MIETEK, f"o:{nr(repo)}"))  # dwa razy ta sama inwestycja
    bot.handle_update(click(MIETEK, f"s1:{nr(repo)}"))
    bot.handle_update(click(MIETEK, f"fu:{nr(repo)}:0"))
    clock.advance(days=10)
    bot.handle_update(click(OBCY, f"o:{nr(repo, 'B/1')}"))  # po 10 dniach – poza raportem 7-dniowym

    bot.handle_update(message(ADMIN, "/raport 7"))
    week = api.last_to(ADMIN)["text"]
    assert "ostatnie 7 dni" in week
    assert "Otwarte szczegóły: 1 inwestycja · 1 osoba" in week

    bot.handle_update(message(ADMIN, "/raport 30"))
    month = api.last_to(ADMIN)["text"]
    assert "Wysłane w raportach i alertach: 2 inwestycje · 2 osoby" in month
    assert "Otwarte szczegóły: 2 inwestycje · 2 osoby" in month
    assert "Zapisane: 1 inwestycja · 1 osoba" in month
    assert "👍 Przydatne: 0 · 👎 Nieprzydatne: 1" in month
    assert "wysłanie to nie przeczytanie" in month and "mapy" in month


def test_setup_trial_start_and_extension_are_recorded(bot, api, repo):
    bot.handle_update(message(OBCY, "/start"))
    bot.handle_update(click(ADMIN, f"adm:trial:{OBCY}"))
    bot.handle_update(click(OBCY, "ob:none"))
    bot.handle_update(click(OBCY, "oa:all"))
    bot.handle_update(click(OBCY, "ts"))
    bot.handle_update(message(ADMIN, f"/przedluz {OBCY} 30"))

    counts = BotStore(repo).event_counts(TODAY - timedelta(days=1))
    assert counts["konfiguracja"] == (1, 0) and counts["test_start"] == (1, 0)
    assert counts["dostep_przedluzony"][0] == 2  # MIETEK (activate) i OBCY
