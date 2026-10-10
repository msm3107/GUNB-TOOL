from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading

import pytest

import main
from gunb_tool.bot_store import BotStore, UserFilters
from gunb_tool.config import BotConfig, EmailConfig, load_config
from gunb_tool.email_bot import VerificationRequests
from gunb_tool.notification_models import DeliveryResult, utc_iso
from gunb_tool.notification_worker import NotificationWorker
from tests.bot_helpers import MIETEK, lead
from tests.test_email_config import ACTIVE, ENV, config_file
from tests.test_notification_store import notification_store, ready, enqueue


class Sender:
    def __init__(self, repo, outcome='accepted'):
        self.repo, self.outcome, self.reports, self.codes = repo, outcome, [], []

    def send(self, endpoint, report, **kwargs):
        assert not self.repo.connection.in_transaction
        self.reports.append((endpoint, report))
        return DeliveryResult(self.outcome)

    def send_verification(self, endpoint, token, **kwargs):
        assert not self.repo.connection.in_transaction
        self.codes.append(token)
        return DeliveryResult('accepted')


def jobs(store, sender=None, **kwargs):
    from gunb_tool.email_runtime import EmailJobs
    sender = sender or Sender(store.repo)
    return EmailJobs(store, VerificationRequests(), sender, BotConfig(), **kwargs), sender


def new_lead(store, clock, name='B/1'):
    clock.advance(seconds=1)
    store.repo.upsert(lead(name))


def test_activation_no_backfill_restart_and_channel_history(notification_store, clock):
    store = notification_store
    ep = ready(store)
    new_lead(store, clock)
    job, sender = jobs(store)
    assert job.run_once() == 1
    report = sender.reports[0][1]
    assert [ref.id_sprawy for ref in report.leads] == ['B/1']
    assert '/email wylacz' in report.body
    assert not store.repo.connection.execute('SELECT 1 FROM deliveries').fetchone()
    assert store.repo.connection.execute('SELECT COUNT(*) FROM notification_deliveries').fetchone()[0] == 1
    restarted, other = jobs(store)
    assert restarted.run_once() == 0 and not other.reports
    assert BotStore(store.repo).job_time('email_worker') == clock.now_utc()


@pytest.mark.parametrize('start', [datetime(2026, 3, 29, 4, 59, tzinfo=timezone.utc),
                                  datetime(2026, 10, 25, 5, 59, tzinfo=timezone.utc)])
def test_daily_schedule_follows_warsaw_and_not_utc(notification_store, clock, start):
    store = notification_store
    clock.advance(seconds=(start - clock.now_utc()).total_seconds())
    BotStore(store.repo).set_access(MIETEK, (start + timedelta(days=3)).isoformat())
    ready(store)
    new_lead(store, clock)
    job, sender = jobs(store)
    assert job.run_once() == 0
    clock.advance(minutes=1)
    assert job.run_once() == 1 and len(sender.reports) == 1


def test_quiet_hours_block_reports_but_allow_requested_code(notification_store, clock):
    store = notification_store
    ep = ready(store)
    enqueue(store, ep)
    unverified = store.add_endpoint(MIETEK, 'email', 'another@example.test')
    job, sender = jobs(store)
    job.requests.submit(MIETEK, unverified.id)
    clock.advance(hours=15)  # 22:00 Warsaw
    assert job.run_once() == 0 and sender.codes and not sender.reports
    clock.advance(hours=8)
    assert job.run_once() == 1 and not sender.reports  # expired claim was handled without SMTP
    assert store.repo.connection.execute('SELECT state FROM notification_outbox').fetchone()[0] == 'expired'


def test_instant_interval_and_mode_change(notification_store, clock):
    store = notification_store
    ep = ready(store)
    ep = store.set_mode(MIETEK, ep.id, 'natychmiast', expected_version=ep.version)
    new_lead(store, clock)
    job, sender = jobs(store)
    job.run_once()
    new_lead(store, clock, 'C/1')
    job.run_once()
    assert len(sender.reports) == 1
    clock.advance(minutes=10)
    job.run_once()
    assert len(sender.reports) == 2


def test_bounded_batches_resume_and_deduplicate(notification_store, clock):
    store = notification_store
    ready(store)
    for i in range(45):
        new_lead(store, clock, f'new/{i}')
    job, sender = jobs(store, max_leads=7)
    for _ in range(9):
        job.run_once()
    refs = [ref for _, part in sender.reports for ref in part.leads]
    assert len(refs) == len(set(refs)) == 45
    assert all(len(part.leads) <= 7 for _, part in sender.reports)


def test_endpoint_paging_does_not_starve_tail(notification_store, clock):
    store = notification_store
    for i in range(12):
        ready(store, address=f'person{i}@example.test')
    new_lead(store, clock)
    job, sender = jobs(store)
    job.run_once()
    assert store.repo.connection.execute('SELECT COUNT(DISTINCT endpoint_id) FROM notification_outbox').fetchone()[0] == 10
    job.run_once()
    assert store.repo.connection.execute('SELECT COUNT(DISTINCT endpoint_id) FROM notification_outbox').fetchone()[0] == 12


@pytest.mark.parametrize('loss', ['paused', 'expired', 'consent', 'address', 'filters'])
def test_queued_reports_recheck_current_state(notification_store, clock, loss):
    store = notification_store
    ep = ready(store)
    enqueue(store, ep)
    bot = BotStore(store.repo)
    if loss == 'paused':
        bot.set_paused(MIETEK, True)
    elif loss == 'expired':
        bot.set_access(MIETEK, (clock.now_utc() - timedelta(days=1)).isoformat())
    elif loss == 'consent':
        store.revoke_consent(MIETEK, ep.id)
    elif loss == 'address':
        store.change_address(MIETEK, ep.id, 'new@example.test')
    else:
        bot.set_filters(MIETEK, UserFilters(powiaty=('9999',)))
    job, sender = jobs(store)
    job.run_once()
    assert not sender.reports


def test_stop_never_claims_or_starts_smtp(notification_store, clock):
    ep = ready(notification_store)
    enqueue(notification_store, ep)
    job, sender = jobs(notification_store, should_stop=lambda: True)
    assert job.run_once() == 0 and not sender.reports
    assert notification_store.repo.connection.execute('SELECT state FROM notification_outbox').fetchone()[0] == 'queued'


def test_retention_preserves_ambiguity_and_dedup_window(notification_store, clock):
    from gunb_tool.email_runtime import prune_notifications
    store = notification_store
    ep = ready(store)
    enqueue(store, ep)
    sender = Sender(store.repo)
    NotificationWorker(store, {'email': sender}).run_once()
    accepted = store.repo.connection.execute('SELECT id FROM notification_outbox').fetchone()[0]
    new_lead(store, clock)
    enqueue(store, ep, 'unknown', 'B/1')
    NotificationWorker(store, {'email': Sender(store.repo, 'unknown')}).run_once()
    before = utc_iso(clock.now_utc() - timedelta(days=40))
    store.repo.connection.execute('UPDATE notification_outbox SET updated_at = ?', (before,))
    BotStore(store.repo).set_job_time('email_schedule:9999', clock.now_utc())
    prune_notifications(store.repo, 30)
    assert store.repo.connection.execute('SELECT state FROM notification_outbox').fetchone()[0] == 'unknown'
    row = store.repo.connection.execute('SELECT outbox_id FROM notification_deliveries').fetchone()
    assert row is not None and row[0] is None  # FK SET NULL; recent dedup record kept.
    assert BotStore(store.repo).job_time('email_schedule:9999') is None
    clock.advance(days=91)
    prune_notifications(store.repo, 30)
    assert store.repo.connection.execute('SELECT COUNT(*) FROM notification_deliveries').fetchone()[0] == 0
    assert store.repo.connection.execute('SELECT COUNT(*) FROM notification_outbox WHERE state = \'unknown\'').fetchone()[0] == 1


def test_verification_retention_seven_days(notification_store, clock):
    from gunb_tool.email_runtime import prune_notifications
    from gunb_tool.email_verification import EmailVerification
    store = notification_store
    ep = store.add_endpoint(MIETEK, 'email', 'new@example.test')
    EmailVerification(store, Sender(store.repo)).request(MIETEK, ep.id)
    clock.advance(days=6)
    prune_notifications(store.repo, 30)
    assert store.repo.connection.execute('SELECT COUNT(*) FROM email_verifications').fetchone()[0] == 1
    clock.advance(days=2)
    prune_notifications(store.repo, 30)
    assert store.repo.connection.execute('SELECT COUNT(*) FROM email_verifications').fetchone()[0] == 0


@pytest.mark.parametrize('flag', ['--bot', '--bot-once'])
def test_bot_dry_run_rejected_before_database(tmp_path, capsys, flag):
    path = config_file(tmp_path, '')
    assert main.main(['--config', str(path), flag, '--dry-run']) == 2
    assert not (tmp_path / 'state.sqlite').exists()


def test_thread_owns_repository_and_does_not_block_ui(notification_store, clock, monkeypatch):
    from gunb_tool import email_runtime
    from gunb_tool.storage import LeadRepository
    from tests.bot_helpers import FakeApi, make_bot, message
    store = notification_store
    ready(store)
    new_lead(store, clock)
    path = config_file(store.repo._file.parent, ACTIVE)
    cfg = load_config(path, env=ENV)
    cfg = replace(cfg, storage=replace(cfg.storage, db_path=store.repo._file))
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    origins = []
    def own_repo(*args, **kwargs):
        origins.append(threading.get_ident())
        return LeadRepository(*args, **kwargs, now=clock.now_utc)
    class Blocking(Sender):
        def __init__(self, *args, **kwargs):
            pass
        def send(self, endpoint, report, **kwargs):
            entered.set()
            assert release.wait(5)
            return DeliveryResult('accepted')
    monkeypatch.setattr(email_runtime, 'LeadRepository', own_repo)
    monkeypatch.setattr(email_runtime, 'SupervisedSMTP', Blocking)
    thread = email_runtime.EmailThread(cfg, VerificationRequests(), stop)
    thread.start()
    try:
        assert entered.wait(3)
        api = FakeApi()
        bot = make_bot(store.repo, api)
        bot.handle_update(message(MIETEK, '/start'))
        assert api.to(MIETEK) and origins == [thread.ident] and thread.ident != threading.get_ident()
    finally:
        stop.set()
        release.set()
        thread.join(5)
    assert not thread.is_alive()


def test_email_health_is_conditional_read_only_and_private(notification_store, tmp_path, clock, monkeypatch):
    from gunb_tool import health
    store = notification_store
    ep = ready(store)
    enqueue(store, ep)
    store.complete(store.claim(['email']), DeliveryResult('unknown'))
    cfg = load_config(config_file(tmp_path, ACTIVE), env=ENV)
    cfg = replace(cfg, storage=replace(cfg.storage, db_path=store.repo._file))
    monkeypatch.setattr(health, 'lock_is_free', lambda path: False)
    before = store.repo.connection.total_changes
    _, checks = health.check_health(cfg, now=clock.now_utc())
    text = '\n'.join(check.text for check in checks)
    assert 'wątek e-mail nie odpowiada' in text and 'wymagają sprawdzenia: 1' in text
    assert ep.address not in text and store.repo.connection.total_changes == before
    BotStore(store.repo).set_job_time('email_worker', clock.now_utc())
    _, checks = health.check_health(cfg, now=clock.now_utc())
    assert any(check.text == 'wątek e-mail działa' for check in checks)
    cfg = replace(cfg, email=EmailConfig())
    _, checks = health.check_health(cfg, now=clock.now_utc())
    assert not any('e-mail' in check.text for check in checks)


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('once', [False, True])
def test_main_email_lifecycle_is_opt_in(notification_store, tmp_path, monkeypatch, enabled, once):
    from types import SimpleNamespace
    store = notification_store
    cfg = load_config(config_file(tmp_path, ACTIVE if enabled else ''), env=ENV)
    counts = dict(email_start=0, email_join=0, cycle=0, sender=0)
    class UI:
        email_commands = None
        def _send(self, *args):
            pass
        def setup(self):
            pass
        def poll_once(self, **kwargs):
            return 0
        def run_forever(self, *, should_stop):
            assert self.email_commands is not None if enabled else self.email_commands is None
            assert should_stop() is False
            stop.set()
    ui = UI()
    stop = threading.Event()
    class Thread:
        def __init__(self, factory, event):
            assert event is stop
        def start(self):
            pass
        def is_alive(self):
            return True
        def join(self, **kwargs):
            pass
    class EmailThread(Thread):
        def __init__(self, config, queue, event):
            super().__init__(None, event)
        def start(self):
            counts['email_start'] += 1
        def join(self, **kwargs):
            counts['email_join'] += 1
    class EmailJobs:
        def __init__(self, *args, **kwargs):
            pass
        def run_once(self):
            counts['cycle'] += 1
    def sender(*args, **kwargs):
        counts['sender'] += 1
        return object()
    monkeypatch.setattr(main.threading, 'Event', lambda: stop)
    monkeypatch.setattr(main, '_stop_on_sigterm', lambda event: None)
    monkeypatch.setattr(main, '_make_bot', lambda *args, **kwargs: ui)
    monkeypatch.setattr(main, '_make_jobs_bot', lambda *args: SimpleNamespace(
        recover_interrupted_import=lambda: None, run_due_jobs=lambda: []))
    monkeypatch.setattr(main, 'JobsWorker', Thread)
    monkeypatch.setattr(main, 'EmailThread', EmailThread)
    monkeypatch.setattr(main, 'EmailJobs', EmailJobs)
    monkeypatch.setattr(main, 'SupervisedSMTP', sender)
    assert main._run_bot_alone(cfg, store.repo, once=once) == 0
    assert counts == dict(email_start=int(enabled and not once), email_join=int(enabled and not once),
                          cycle=int(enabled and once), sender=int(enabled and once))


def test_retention_runs_even_when_email_disabled(notification_store, tmp_path, monkeypatch):
    cfg = load_config(config_file(tmp_path, ''), env={})
    calls = []
    monkeypatch.setattr(main, 'prune_notifications', lambda repo, days: calls.append((repo, days)))
    monkeypatch.setattr(main, 'backup_if_due', lambda *args, **kwargs: None)
    assert main.scheduled_backup(notification_store.repo, cfg) is None
    assert calls == [(notification_store.repo, cfg.notifications.max_age_days)]


def test_powiat_no_match_does_not_decode_every_investment(notification_store, clock, monkeypatch):
    from gunb_tool import notification_store as module
    store = notification_store
    ep = ready(store)
    for i in range(50):
        new_lead(store, clock, f'no-match/{i}')
    BotStore(store.repo).set_filters(MIETEK, UserFilters(powiaty=('9999',)))
    decoded = []
    original = module.investment_from_row
    def decode(row):
        decoded.append(row['id_sprawy'])
        return original(row)
    monkeypatch.setattr(module, 'investment_from_row', decode)
    assert store.candidates(MIETEK, ep.id, clock.now_utc() - timedelta(days=1)) == []
    assert not decoded


def test_powiat_prefilter_preserves_place_alternative(notification_store, clock):
    store = notification_store
    ep = ready(store)
    new_lead(store, clock)
    store.repo.upsert(lead('B/1', miejscowosc='Kostrzyn'))
    BotStore(store.repo).set_filters(MIETEK, UserFilters(powiaty=('9999',), miejsca=('Kostrzyn',)))
    assert [inv.id_sprawy for inv in store.candidates(MIETEK, ep.id, clock.now_utc() - timedelta(days=1))] == ['A/1', 'B/1']


def test_report_split_reserves_unsubscribe_footer(notification_store, monkeypatch):
    from gunb_tool import notification_reports
    from gunb_tool.email_runtime import FOOTER
    store = notification_store
    endpoint = ready(store)
    investments = [lead(str(i)) for i in range(20)]
    for inv in investments:
        store.repo.upsert(inv)
    monkeypatch.setattr(notification_reports, 'MAX_BODY_BYTES', 700)
    parts = notification_reports.build_report(endpoint, [store.repo.get(inv.id_sprawy) for inv in investments],
                                             now=store.repo.now(), footer=FOOTER)
    assert len(parts) > 1 and sum(len(part.leads) for part in parts) == 20
    assert all(part.body.endswith(FOOTER) and len(part.body.encode('utf-8')) <= 700 for part in parts)


def test_small_budget_rotates_actual_recipients_under_continuous_load(notification_store, clock):
    store = notification_store
    bot = BotStore(store.repo)
    chats = set(range(4000, 4012))  # More recipients than one fetched page.
    for chat in sorted(chats):
        bot.register(chat, 'Synthetic', None, status='aktywny', backlog_days=7)
        bot.set_access(chat, (clock.now_utc() + timedelta(days=2)).isoformat())
        ep = store.add_endpoint(chat, 'email', f'person{chat}@example.test')
        ep = store.record_consent(chat, ep.id, expected_version=ep.version, source='test')
        ep = store.record_verification(chat, ep.id, expected_version=ep.version)
        store.set_enabled(chat, ep.id, True, expected_version=ep.version)
    job, sender = jobs(store, max_leads=1)
    for i in range(12):
        new_lead(store, clock, f'continuous/{i}')
        job.run_once()
    assert {ep.chat_id for ep, _ in sender.reports} == chats


def test_quiet_boundary_rechecked_after_scheduling(notification_store, clock, monkeypatch):
    from tests.test_notification_store import report
    store = notification_store
    clock.utc = datetime(2026, 10, 10, 19, 59, 58, tzinfo=timezone.utc)  # 21:59:58 Warsaw
    BotStore(store.repo).set_access(MIETEK, (clock.now_utc() + timedelta(days=2)).isoformat())
    ep = ready(store)
    store.enqueue(MIETEK, ep.id, 'quiet-boundary', report(store, ep),
                  expires_at=clock.now_utc() + timedelta(hours=24))
    job, sender = jobs(store)
    monkeypatch.setattr(job, '_schedule', lambda now: clock.advance(seconds=5))
    assert job.run_once() == 0 and not sender.reports
    assert store.repo.connection.execute('SELECT state FROM notification_outbox').fetchone()[0] == 'queued'
    clock.advance(hours=8)
    assert job.run_once() == 1 and len(sender.reports) == 1
