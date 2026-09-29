"""Warstwa doręczania (Telegram): limit 429 „FloodWait” i klienci, którzy zablokowali bota."""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pytest
import responses

from gunb_tool.bot import LeadBot
from gunb_tool.bot_store import BotStore
from gunb_tool.config import BotConfig
from gunb_tool.exporter import MessageFormatter, OutgoingMessage, TelegramNotifier
from gunb_tool.http_client import MAX_RETRY_AFTER
from gunb_tool.telegram_api import TelegramApiError
from tests.qa.conftest import BOT_TOKEN, TELEGRAM_SEND, sent_payloads, slept, telegram_error, telegram_ok

FLOOD_WAIT = telegram_error(429, "Too Many Requests: retry after 7", retry_after=7)
BLOCKED = telegram_error(403, "Forbidden: bot was blocked by the user")
CREWS = (101, 202, 303)


@pytest.fixture
def bot(memory_repo, telegram_api) -> LeadBot:
    settings = BotConfig(admins=(1,), access="open", fetch_times=(), morning_time="07:00", evening_time="19:00")
    return LeadBot(memory_repo, telegram_api, settings=settings, powiat_codes=("1607",), formatter=MessageFormatter(),
                   clock=lambda: datetime(2026, 9, 29, 7, 5))


@pytest.fixture
def store(memory_repo) -> BotStore:
    store = BotStore(memory_repo)
    for chat in CREWS:
        store.register(chat, f"Ekipa {chat}", None, status="aktywny", backlog_days=7)
    return store


def reply_per_chat(blocked: set[int]):
    """Odpowiedź Telegrama zależna od odbiorcy: 403 dla tych, którzy zablokowali bota."""
    def callback(request):
        chat = json.loads(request.body)["chat_id"]
        if chat in blocked:
            return 403, {}, json.dumps(BLOCKED)
        return 200, {}, json.dumps(telegram_ok())
    return callback


# --- 429 Too Many Requests („FloodWait”) ---------------------------------------------------------------

def test_flood_wait_pauses_the_queue_for_the_time_telegram_asks(telegram_api, http, sleep):
    http.add(responses.POST, TELEGRAM_SEND, json=FLOOD_WAIT, status=429)
    http.add(responses.POST, TELEGRAM_SEND, json=telegram_ok(5))

    result = telegram_api.send_message(42, "🏗️ Nowy lead")

    assert result["message_id"] == 5
    assert len(http.calls) == 2
    assert slept(sleep) == [7.0]  # dokładnie tyle, ile kazał Telegram (parameters.retry_after)


def test_queue_keeps_order_and_one_message_per_second_after_flood_wait(telegram_api, http, sleep):
    http.add(responses.POST, TELEGRAM_SEND, json=telegram_ok(1))
    http.add(responses.POST, TELEGRAM_SEND, json=FLOOD_WAIT, status=429)
    http.add(responses.POST, TELEGRAM_SEND, json=telegram_ok(2))
    http.add(responses.POST, TELEGRAM_SEND, json=telegram_ok(3))

    for text in ("Lead 1", "Lead 2", "Lead 3"):
        telegram_api.send_message(42, text)

    assert [payload["text"] for payload in sent_payloads(http)] == ["Lead 1", "Lead 2", "Lead 2", "Lead 3"]
    assert slept(sleep) == [1.0, 7.0, 1.0]  # odstęp 1 s na czat, przerwa „FloodWait”, znów 1 s


def test_retry_after_header_is_honoured_too(telegram_api, http, sleep):
    http.add(responses.POST, TELEGRAM_SEND, json={"ok": False, "error_code": 429}, status=429,
             headers={"Retry-After": "3"})
    http.add(responses.POST, TELEGRAM_SEND, json=telegram_ok())
    telegram_api.send_message(42, "x")
    assert slept(sleep) == [3.0]


def test_absurd_retry_after_is_capped(telegram_api, http, sleep):
    http.add(responses.POST, TELEGRAM_SEND, json=telegram_error(429, "Too Many Requests", retry_after=86400),
             status=429)
    http.add(responses.POST, TELEGRAM_SEND, json=telegram_ok())
    telegram_api.send_message(42, "x")
    assert slept(sleep) == [MAX_RETRY_AFTER]  # najwyżej 10 minut, nie doba


def test_persistent_flood_wait_ends_with_error_and_crew_stays_active(bot, store, memory_repo, make_lead, http,
                                                                      caplog):
    for chat in CREWS[1:]:
        store.set_status(chat, "odrzucony")  # w tym teście tylko jedna ekipa
    memory_repo.upsert(make_lead())
    http.add(responses.POST, TELEGRAM_SEND, json=FLOOD_WAIT, status=429)

    with caplog.at_level(logging.WARNING):
        assert bot.deliver_reports("rano") == 0

    assert len(http.calls) == 4  # 1 próba + 3 ponowienia
    assert store.get_user(101).status == "aktywny"  # 429 to nie blokada – nie wyłączamy klienta
    assert "429" in caplog.text


def test_channel_notifier_waits_out_flood_wait(make_http, http, sleep, clock):
    notifier = TelegramNotifier(make_http(circuit_breaker_failures=0), BOT_TOKEN, "-100777", sleep=sleep, clock=clock)
    http.add(responses.POST, TELEGRAM_SEND, json=FLOOD_WAIT, status=429)
    http.add(responses.POST, TELEGRAM_SEND, json=telegram_ok())

    notifier.send(OutgoingMessage(text="<b>Lead</b>"))

    assert 7.0 in slept(sleep)
    assert [payload["chat_id"] for payload in sent_payloads(http)] == ["-100777", "-100777"]


# --- 403 „Forbidden: bot was blocked by the user” ------------------------------------------------------

def test_blocked_crew_is_flagged_and_others_still_get_their_report(bot, store, memory_repo, make_lead, http):
    memory_repo.upsert(make_lead())
    http.add_callback(responses.POST, TELEGRAM_SEND, callback=reply_per_chat(blocked={202}),
                      content_type="application/json")

    delivered = bot.deliver_reports("rano")

    assert delivered == 2
    assert store.get_user(202).status == "zablokowany"
    assert store.get_user(101).status == store.get_user(303).status == "aktywny"
    assert sorted(payload["chat_id"] for payload in sent_payloads(http)) == [101, 202, 303]


def test_blocked_crew_is_skipped_in_the_next_rounds(bot, store, memory_repo, make_lead, http):
    memory_repo.upsert(make_lead("A/1"))
    http.add_callback(responses.POST, TELEGRAM_SEND, callback=reply_per_chat(blocked={202}),
                      content_type="application/json")
    bot.deliver_reports("rano")
    first_round = len(http.calls)
    memory_repo.upsert(make_lead("B/1"))

    bot.deliver_reports("rano")

    later = [json.loads(call.request.body)["chat_id"] for call in list(http.calls)[first_round:]]
    assert sorted(later) == [101, 303]  # do zablokowanego już nie piszemy


def test_instant_delivery_loop_survives_a_blocked_crew(bot, store, memory_repo, make_lead, http):
    for chat in CREWS:
        store.set_mode(chat, "natychmiast")
    memory_repo.upsert(make_lead())
    http.add_callback(responses.POST, TELEGRAM_SEND, callback=reply_per_chat(blocked={101}),
                      content_type="application/json")

    sent = bot.deliver_instant()

    assert sent == 2
    assert store.get_user(101).status == "zablokowany"
    assert [store.get_user(chat).status for chat in (202, 303)] == ["aktywny", "aktywny"]


def test_crew_that_unblocks_and_writes_again_is_reactivated(bot, store, http):
    store.set_status(202, "zablokowany")
    http.add(responses.POST, TELEGRAM_SEND, json=telegram_ok())

    bot.handle_update({"update_id": 1, "message": {"message_id": 1, "text": "📊 Co nowego?",
                                                   "chat": {"id": 202, "type": "private"},
                                                   "from": {"id": 202, "first_name": "Ekipa"}}})

    assert store.get_user(202).status == "aktywny"


def test_blocked_error_is_recognised_from_telegram_response(telegram_api, http):
    http.add(responses.POST, TELEGRAM_SEND, json=BLOCKED, status=403)
    with pytest.raises(TelegramApiError) as excinfo:
        telegram_api.send_message(202, "x")
    assert excinfo.value.blocked is True
    assert len(http.calls) == 1  # 403 nie jest ponawiane
