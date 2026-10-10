from datetime import timedelta

import pytest

from gunb_tool.bot_store import BotStore
from gunb_tool.email_verification import EmailVerification
from gunb_tool.notification_models import DeliveryResult
from tests.bot_helpers import MIETEK, OBCY, FakeApi, make_bot, message
from tests.test_notification_store import notification_store, ready, enqueue


@pytest.fixture
def commands(notification_store):
    from gunb_tool.email_bot import EmailCommands, VerificationRequests
    replies = []
    requests = VerificationRequests()
    helper = EmailCommands(notification_store, requests, lambda chat, text: replies.append((chat, text)))
    return helper, requests, replies


def run(commands, store, text='', chat=MIETEK):
    commands[0].handle(BotStore(store.repo).get_user(chat), text.split())
    return commands[2][-1][1]


def test_setup_queues_only_ids_no_proof_or_consent(commands, notification_store):
    store = notification_store
    run(commands, store, 'ustaw person@example.test')
    ep, = store.list_endpoints(MIETEK)
    assert commands[1].take() == (MIETEK, ep.id)
    assert ep.mode == 'rano' and not ep.enabled and ep.verified_at is None and ep.consent_at is None
    assert store.repo.connection.execute('SELECT COUNT(*) FROM email_verifications').fetchone()[0] == 0
    assert 'person@example.test' not in run(commands, store)


class Capture:
    def send_verification(self, endpoint, token, **kwargs):
        self.token = token
        return DeliveryResult('accepted')


def test_code_requires_separate_consent(commands, notification_store):
    store = notification_store
    run(commands, store, 'ustaw person@example.test')
    ep, = store.list_endpoints(MIETEK)
    capture = Capture()
    EmailVerification(store, capture).request(MIETEK, ep.id)
    run(commands, store, 'potwierdz ' + capture.token)
    ep = store.get_endpoint(MIETEK, ep.id)
    assert ep.verified_at and not ep.enabled and ep.consent_at is None
    assert capture.token not in commands[2][-1][1]
    run(commands, store, 'zgoda')
    ep = store.get_endpoint(MIETEK, ep.id)
    assert ep.enabled and ep.consent_source == 'telegram:/email zgoda:v1'


def test_no_consent_without_proof(commands, notification_store):
    run(commands, notification_store, 'ustaw person@example.test')
    run(commands, notification_store, 'zgoda')
    ep, = notification_store.list_endpoints(MIETEK)
    assert not ep.enabled and ep.consent_at is None


@pytest.mark.parametrize('verb', ['ustaw new@example.test', 'ponow', 'potwierdz abc', 'zgoda', 'tryb wieczor'])
@pytest.mark.parametrize('reason', ['expired', 'paused', 'rejected'])
def test_settings_require_current_access(commands, notification_store, clock, verb, reason):
    store = notification_store
    ep = ready(store)
    bot = BotStore(store.repo)
    if reason == 'expired':
        bot.set_access(MIETEK, (clock.now_utc() - timedelta(days=1)).isoformat())
    elif reason == 'paused':
        bot.set_paused(MIETEK, True)
    else:
        bot.set_status(MIETEK, 'odrzucony')
    run(commands, store, verb)
    assert store.get_endpoint(MIETEK, ep.id) == ep


def test_off_and_delete_reachable_without_access(commands, notification_store, clock):
    store = notification_store
    ep = ready(store)
    enqueue(store, ep)
    BotStore(store.repo).set_access(MIETEK, (clock.now_utc() - timedelta(days=1)).isoformat())
    BotStore(store.repo).set_paused(MIETEK, True)
    run(commands, store, 'wylacz')
    assert not store.get_endpoint(MIETEK, ep.id).enabled
    assert store.repo.connection.execute('SELECT state FROM notification_outbox').fetchone()[0] == 'cancelled'
    run(commands, store, 'usun')
    assert not store.list_endpoints(MIETEK)


def test_mode_cancels_stale_queue_and_address_resets_proofs(commands, notification_store):
    store = notification_store
    ep = ready(store)
    enqueue(store, ep)
    run(commands, store, 'tryb wieczor')
    changed = store.get_endpoint(MIETEK, ep.id)
    assert changed.mode == 'wieczor' and changed.version == ep.version + 1
    assert store.repo.connection.execute('SELECT state FROM notification_outbox').fetchone()[0] == 'cancelled'
    run(commands, store, 'ustaw new@example.test')
    changed = store.get_endpoint(MIETEK, ep.id)
    assert changed.address == 'new@example.test' and not changed.enabled
    assert changed.verified_at is None and changed.consent_at is None


def test_unknown_quarantine_cannot_be_reenabled_by_consent(commands, notification_store):
    store = notification_store
    ep = ready(store)
    enqueue(store, ep)
    claim = store.claim(channels=['email'])
    store.complete(claim, DeliveryResult('unknown'))
    assert 'administrat' in run(commands, store, 'zgoda').lower()
    assert not store.get_endpoint(MIETEK, ep.id).enabled


def test_owned_email_only_and_multiple_requires_operator(commands, notification_store):
    store = notification_store
    ready(store, 'whatsapp', '+48123456789')
    BotStore(store.repo).register(OBCY, 'Other', None, status='aktywny', backlog_days=7)
    foreign = store.add_endpoint(OBCY, 'email', 'other@example.test')
    run(commands, store, 'usun')
    assert store.get_endpoint(OBCY, foreign.id)
    run(commands, store, 'ustaw first@example.test')
    store.add_endpoint(MIETEK, 'email', 'second@example.test')
    assert 'administrat' in run(commands, store, 'usun').lower()
    assert len(store.list_endpoints(MIETEK)) == 2


def test_bot_hook_is_private_and_unsubscribe_bypasses_gate(commands, notification_store):
    store = notification_store
    ep = ready(store)
    api = FakeApi()
    bot = make_bot(store.repo, api)
    bot.email_commands = commands[0]
    group = message(MIETEK, '/email usun')
    group['message']['chat']['type'] = 'group'
    bot.handle_update(group)
    assert store.get_endpoint(MIETEK, ep.id)
    BotStore(store.repo).set_status(MIETEK, 'odrzucony')
    bot.handle_update(message(MIETEK, '/email wylacz'))
    assert not store.get_endpoint(MIETEK, ep.id).enabled


def test_queue_bound_ttl_dedup_and_inflight():
    from gunb_tool.email_bot import VerificationRequests
    now = [0.0]
    queue = VerificationRequests(_clock=lambda: now[0])
    assert queue.submit(1, 10) and not queue.submit(1, 11)
    assert queue.take() == (1, 10)
    assert not queue.submit(1, 11)
    queue.done(1)
    for chat in range(1, 26):
        assert queue.submit(chat, chat)
    assert not queue.submit(26, 26)
    now[0] = 61
    assert queue.take() is None
    assert queue.submit(26, 26)
    assert VerificationRequests().take() is None


@pytest.mark.parametrize('text', ['ustaw bad\r\naddress', 'ustaw a@b c@d', 'tryb invalid', 'potwierdz abc', 'admin 42'])
def test_invalid_input_is_generic(commands, notification_store, text):
    reply = run(commands, notification_store, text)
    assert text not in reply and 'Traceback' not in reply
