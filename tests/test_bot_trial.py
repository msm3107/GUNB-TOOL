"""P1: trial i powroty – jedna spokojna podpowiedź po 48 h bez efektów (z ciszą nocną, pauzą i wyłącznikiem),
podsumowanie z danych, jednorazowe przedłużenie testu przez admina z powodem, brak samodzielnego resetu."""

from datetime import date, datetime, timedelta, timezone

import pytest

from gunb_tool.bot_store import BotStore
from gunb_tool.clock import WARSAW
from gunb_tool.funnel import ACTIVATION, activation_time
from tests.bot_helpers import ADMIN, MIETEK, buttons, click, configured, lead, make_bot, message

NUDGE = "💡 <b>Jak idzie test?</b>"


def decided(days_ago: int) -> dict:
    day = (date(2026, 9, 29) - timedelta(days=days_ago)).isoformat()
    return dict(data_decyzji=day, data_aktualizacji=day)


@pytest.fixture
def bot(repo, api, clock):
    bot = make_bot(repo, api, clock)
    for n in range(4):
        repo.upsert(lead(f"T/{n}", nazwa_zamierzenia=f"Budowa {n}", **decided(n + 1)))
    bot.handle_update(message(MIETEK, "/start"))
    configured(bot)
    bot.handle_update(click(ADMIN, f"adm:trial:{MIETEK}"))
    bot.handle_update(click(MIETEK, "ts"))  # start 29.09 07:00
    api.sent.clear()
    return bot


def store(repo):
    return BotStore(repo)


def at(clock, day: int, hour: int, minute: int = 0) -> None:
    """Październikowy dzień i godzina czasu polskiego."""
    clock.utc = datetime(2026, 10, day, hour, minute, tzinfo=WARSAW).astimezone(timezone.utc)


def nudges(api):
    return [m for m in api.to(MIETEK) if m["text"].startswith(NUDGE)]


def test_one_calm_nudge_two_days_after_start_without_activation(bot, api, repo, clock):
    at(clock, 1, 9)  # 01.10, 09:00 – 50 h po starcie
    bot.run_due_jobs()
    clock.advance(hours=3)
    bot.run_due_jobs()
    at(clock, 2, 9)
    bot.run_due_jobs()

    assert len(nudges(api)) == 1
    nudge = nudges(api)[0]
    assert "Nie otwarto jeszcze żadnej inwestycji" in nudge["text"] and "06.10.2026, 07:00" in nudge["text"]
    assert {("📊 Pokaż inwestycje", "f:go"), ("💬 Napisz do nas", "zm:q")} <= set(buttons(nudge["markup"]))
    assert "wyłączysz w ⚙️ Ustawienia" in nudge["text"]


def test_activated_person_gets_no_nudge(bot, api, repo, clock):
    for n in range(3):
        bot.handle_update(click(MIETEK, f"o:{repo.get(f'T/{n}').nr}"))
    bot.handle_update(click(MIETEK, f"s1:{repo.get('T/0').nr}"))
    at(clock, 1, 9)
    bot.run_due_jobs()
    assert nudges(api) == []
    start = datetime.fromisoformat(store(repo).get_user(MIETEK).test_start)
    assert activation_time(store(repo).events(), start, ACTIVATION) is not None


@pytest.mark.parametrize("blocker", ["pause", "tips_off", "night"])
def test_nudge_respects_pause_tips_setting_and_night(bot, api, repo, clock, blocker):
    if blocker == "pause":
        store(repo).set_paused(MIETEK, True)
    elif blocker == "tips_off":
        bot.handle_update(click(MIETEK, "st:t"))  # ⚙️ Ustawienia → 💡 Podpowiedzi: wyłącz
    at(clock, 1, 23, 30) if blocker == "night" else at(clock, 1, 9)  # ponad 48 h po starcie
    bot.run_due_jobs()
    assert nudges(api) == []
    if blocker == "night":
        at(clock, 2, 9)
        bot.run_due_jobs()
        assert len(nudges(api)) == 1  # rano – raz


def test_reminder_summary_counts_real_actions_and_never_claims_reading(bot, api, repo, clock):
    bot.handle_update(click(MIETEK, f"o:{repo.get('T/0').nr}"))
    bot.handle_update(click(MIETEK, f"o:{repo.get('T/1').nr}"))
    bot.handle_update(click(MIETEK, f"s1:{repo.get('T/0').nr}"))
    at(clock, 5, 10)  # dzień przed końcem testu
    bot.run_due_jobs()
    reminder = next(m for m in api.to(MIETEK) if "kończy się" in m["text"])
    assert "otwarte szczegóły: 2" in reminder["text"] and "zapisane: 1" in reminder["text"]
    assert "przeczyt" not in reminder["text"].lower()


# --- Przedłużenie testu przez admina --------------------------------------------------------------------

def test_admin_extends_a_trial_once_with_a_reason(bot, api, repo):
    before = store(repo).get_user(MIETEK).subscription_ends

    bot.handle_update(message(ADMIN, f"/przedluztest {MIETEK} 3 klient na urlopie"))
    bot.handle_update(message(ADMIN, f"/przedluztest {MIETEK} 3 jeszcze raz"))

    person = store(repo).get_user(MIETEK)
    expected = (datetime.fromisoformat(before) + timedelta(days=3)).isoformat(timespec="seconds")
    assert person.subscription_ends == person.test_koniec == expected
    assert person.test_przedluzenie_powod == "klient na urlopie" and person.on_trial
    assert "09.10.2026" in api.last_to(MIETEK)["text"] and "przedłużony" in api.last_to(MIETEK)["text"]
    assert "już był przedłużany" in api.last_to(ADMIN)["text"]
    assert [e.szczegoly for e in store(repo).events(kinds=("test_przedluzony",))] == ["3d"]


@pytest.mark.parametrize("command", [f"/przedluztest {MIETEK} 3", f"/przedluztest {MIETEK} 30 za długo",
                                     f"/przedluztest {MIETEK} zero powód", "/przedluztest"])
def test_trial_extension_needs_days_up_to_14_and_a_reason(bot, api, repo, command):
    before = store(repo).get_user(MIETEK).subscription_ends
    bot.handle_update(message(ADMIN, command))
    assert "Użycie" in api.last_to(ADMIN)["text"]
    assert store(repo).get_user(MIETEK).subscription_ends == before


def test_paid_access_is_not_a_trial_to_extend(bot, api, repo):
    bot.handle_update(message(ADMIN, f"/aktywuj {MIETEK} 30"))
    bot.handle_update(message(ADMIN, f"/przedluztest {MIETEK} 3 powód"))
    assert "nie jest w teście" in api.last_to(ADMIN)["text"]


def test_person_cannot_reset_the_trial_themselves(bot, api, repo, clock):
    clock.advance(days=8)
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(click(MIETEK, "i:test"))
    bot.handle_update(click(MIETEK, "ts"))
    person = store(repo).get_user(MIETEK)
    assert not bot._has_access(person) and person.test_koniec == "2026-10-06T05:00:00+00:00"
    assert not any(m["text"].startswith("🙋 Prośba o test") for m in api.to(ADMIN))


def test_tips_switch_lives_in_settings(bot, api, repo):
    bot.handle_update(message(MIETEK, "⚙️ Ustawienia"))
    screen = api.last_to(MIETEK)
    assert "💡 Podpowiedzi i podsumowania: włączone" in screen["text"]
    bot.handle_update(click(MIETEK, "st:t", message_id=screen["message_id"]))
    assert not store(repo).get_user(MIETEK).tips_enabled
    assert "💡 Podpowiedzi i podsumowania: wyłączone" in api.edits[-1]["text"]
