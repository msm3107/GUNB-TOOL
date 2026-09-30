"""Lista kontrolna pilotażu: migracja bazy z produkcji (v6) z restartem, rozdzielenie danych dwóch osób,
HTML, emoji i bardzo długie opisy w każdym miejscu, gdzie trafiają do wiadomości."""

import sqlite3

import pytest

from gunb_tool import storage
from gunb_tool.bot_store import BotStore, LeadFlags
from gunb_tool.exporter import TELEGRAM_LIMIT
from gunb_tool.storage import LeadRepository
from tests.bot_helpers import MIETEK, OBCY, activate, click, lead, make_bot, message
from tests.test_storage import legacy_database

NASTY = "<script>alert(1)</script> & „Dom” 🏠🔥 " + "bardzo długi opis " * 400


# --- Migracja bazy z main (v6: abonamenty) na obecny schemat -----------------------------------------------

def v6_database(path):
    legacy = legacy_database(path, 6)
    rows = [
        (MIETEK, "aktywny", 1, "2026-10-20T05:00:00+00:00", '{"powiaty": ["2862"]}', "dach"),  # płacący
        (OBCY, "aktywny", 0, None, "{}", None),  # zarejestrowany po wprowadzeniu abonamentów, bez dostępu
        (4004, "zablokowany", 1, "2026-10-20T05:00:00+00:00", "{}", None),
    ]
    for chat_id, status, active, ends, filters, trade in rows:
        legacy.execute("INSERT INTO bot_users (chat_id, status, nowe_od, utworzono, zmieniono, is_active,"
                       " subscription_ends, filtry, branza) VALUES (?, ?, '2026-09-01T00:00:00+00:00', 'x', 'x',"
                       " ?, ?, ?, ?)", (chat_id, status, active, ends, filters, trade))
    legacy.execute("INSERT INTO investments (id_sprawy, zrodlo, status, utworzono, zmieniono, status_zmieniony,"
                   " ostatnio_widziany, nr) VALUES ('A/1', 'pozwolenia', 'decyzja', 'x', 'x', 'x', 'x', 1)")
    legacy.execute("INSERT INTO user_leads (chat_id, id_sprawy, stan, zmieniono) VALUES (?, 'A/1', 'zapisany', 'x')",
                   (MIETEK,))
    legacy.execute("INSERT INTO deliveries (chat_id, id_sprawy, rewizja, rodzaj, doreczono)"
                   " VALUES (?, 'A/1', 'x', 'raport', '2026-09-28T05:00:00+00:00')", (MIETEK,))
    legacy.execute("INSERT INTO watchlist (chat_id, rodzaj, wartosc, etykieta, utworzono)"
                   " VALUES (?, 'gmina', '2862011', 'Olsztyn', 'x')", (MIETEK,))
    legacy.executemany("INSERT INTO bot_jobs (nazwa, ostatnio) VALUES (?, ?)",
                       [("telegram_offset", "777"), ("raport_rano", "2026-09-29T07:00:03")])
    legacy.commit()
    legacy.close()


def test_production_database_migrates_keeps_everything_and_survives_a_restart(tmp_path, api, clock):
    path = tmp_path / "prod.sqlite"
    v6_database(path)

    for attempt in range(2):  # pierwszy start (migracja) i restart po migracji
        with LeadRepository(path, now=clock.now_utc) as repo:
            assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
            store = BotStore(repo)
            bot = make_bot(repo, api, clock)
            mietek, obcy, blocked = (store.get_user(c) for c in (MIETEK, OBCY, 4004))
            assert bot._has_access(mietek) and not bot._has_access(obcy)  # abonament zostaje, nikt nie zyskuje
            assert mietek.subscription_ends == "2026-10-20T05:00:00+00:00" and mietek.setup_done
            assert mietek.filtry.powiaty == ("2862",) and mietek.branza == "dach"
            assert store.lead_flags(MIETEK, "A/1") == LeadFlags(saved=True)
            assert [w.etykieta for w in store.watchlist(MIETEK)] == ["Olsztyn"]
            assert store.job_last_run("telegram_offset") == "777"
            assert store.job_time("raport_rano") is not None
            assert blocked.status == "zablokowany" and not blocked.wstrzymane


def test_code_from_main_still_reads_the_migrated_database(tmp_path, clock):
    """Wycofanie zmian: zapytania kodu z main (v6) działają na bazie po migracji (schemat tylko dodaje)."""
    path = tmp_path / "prod.sqlite"
    v6_database(path)
    with LeadRepository(path, now=clock.now_utc) as repo:
        BotStore(repo).set_lead_flags(MIETEK, "A/1", reviewed=True)
    old = sqlite3.connect(path)
    old.row_factory = sqlite3.Row
    subscribers = old.execute("SELECT chat_id FROM bot_users WHERE status = 'aktywny'"
                              " AND ((is_active = 1 AND subscription_ends > ?) OR chat_id IN (1001))",
                              ("2026-09-29T05:00:00+00:00",)).fetchall()
    assert [row["chat_id"] for row in subscribers] == [MIETEK]
    assert old.execute("SELECT stan FROM user_leads WHERE chat_id = ?", (MIETEK,)).fetchone()["stan"] == "zapisany"
    assert old.execute("SELECT ostatnio FROM bot_jobs WHERE nazwa = 'telegram_offset'").fetchone()[0] == "777"
    old.close()


# --- Dwie osoby, dwa światy ------------------------------------------------------------------------------------------

def test_two_people_never_see_each_others_marks_notes_or_reminders(repo, api, clock):
    bot = make_bot(repo, api, clock)
    activate(bot, api, MIETEK)
    activate(bot, api, OBCY)
    repo.upsert(lead("A/1", nazwa_zamierzenia="Wspólna inwestycja"))
    nr = repo.get("A/1").nr

    bot.handle_update(click(MIETEK, f"s1:{nr}"))
    bot.handle_update(click(MIETEK, f"pr:{nr}:7"))
    bot.handle_update(click(MIETEK, f"nt:{nr}"))
    bot.handle_update(message(MIETEK, "tajne ustalenia"))
    bot.handle_update(click(OBCY, f"h:{nr}"))

    store = BotStore(repo)
    assert store.lead_flags(OBCY, "A/1") == LeadFlags(hidden=True)
    assert store.lead_flags(MIETEK, "A/1") == LeadFlags(saved=True)  # ukrycie przez drugą osobę nic nie zmienia
    assert store.note(OBCY, "A/1") is None and store.reminder(OBCY, "A/1") is None
    bot.handle_update(message(MIETEK, "⭐ Zapisane"))
    assert "Wspólna inwestycja" in api.last_to(MIETEK)["text"]
    bot.handle_update(message(OBCY, "⭐ Zapisane"))
    assert "Wspólna inwestycja" not in api.last_to(OBCY)["text"]


# --- HTML, emoji i bardzo długie opisy -----------------------------------------------------------------------------

@pytest.fixture
def nasty_bot(repo, api, clock):
    bot = make_bot(repo, api, clock)
    activate(bot, api)
    repo.upsert(lead("X/1", nazwa_zamierzenia=NASTY, adres_opisowy="<b>ul. Krzywa</b> & 🏠", priorytet="hot",
                     inwestor="<i>Spółka</i> & Syn 🏗️", data_decyzji="2026-05-15"))
    return bot


def assert_safe(text):
    assert "<script>" not in text and "<i>Spółka</i>" not in text and "<b>ul. Krzywa</b>" not in text
    assert len(text) <= TELEGRAM_LIMIT


def test_html_emoji_and_long_descriptions_are_safe_everywhere(nasty_bot, api, repo, clock):
    bot = nasty_bot
    nr = repo.get("X/1").nr
    bot.handle_update(message(MIETEK, "📊 Inwestycje"))  # raport
    bot.handle_update(message(MIETEK, "📊 Inwestycje"))  # przegląd historii
    bot.handle_update(click(MIETEK, f"o:{nr}"))  # karta
    bot.handle_update(click(MIETEK, f"s1:{nr}"))
    bot.handle_update(message(MIETEK, "⭐ Zapisane"))
    BotStore(repo).set_trade(MIETEK, "dach")
    bot.deliver_stage_reminders()
    bot.handle_update(click(MIETEK, f"h:{nr}"))  # ukryta karta

    texts = [m["text"] for m in api.to(MIETEK)] + [e["text"] for e in api.edits if e["text"]]
    assert len(texts) >= 5
    for text in texts:
        assert_safe(text)
    assert any("🏠" in text for text in texts)  # emoji przechodzą bez zmian


def test_long_card_with_a_long_note_is_shortened_without_breaking_html(repo, api, clock):
    bot = make_bot(repo, api, clock)
    activate(bot, api)
    long_fields = dict(nazwa_zamierzenia="Opis " * 300, adres_opisowy="Adres & " * 250, inwestor="Spółka & " * 120,
                       organ="Starosta & " * 60)
    repo.upsert(lead("L/1", **long_fields))
    nr = repo.get("L/1").nr
    bot.handle_update(click(MIETEK, f"nt:{nr}"))
    bot.handle_update(message(MIETEK, "&" * 300))  # po escapowaniu 1500 znaków

    card = api.last_to(MIETEK)["text"]
    assert len(card) <= TELEGRAM_LIMIT
    assert card.count("<b>") == card.count("</b>") and card.count("<code>") == card.count("</code>")
    assert "📝 Twoja notatka:" in card
    tail = card.rsplit("📝 Twoja notatka:", 1)[1]
    assert "&" not in tail.replace("&amp;", "")  # żadnej uciętej encji


def test_repeated_reminder_and_note_clicks_are_harmless(nasty_bot, api, repo):
    nr = repo.get("X/1").nr
    for _ in range(3):
        nasty_bot.handle_update(click(MIETEK, f"pr:{nr}:14"))
        nasty_bot.handle_update(click(MIETEK, f"nt:{nr}:d"))
    assert repo.connection.execute("SELECT COUNT(*) FROM przypomnienia").fetchone()[0] == 1
    assert BotStore(repo).note(MIETEK, "X/1") is None
