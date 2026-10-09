"""Bounded email scheduling, its dedicated thread and metadata retention (schema v14)."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import logging
import threading

from .bot_store import BotStore
from .clock import at_local_time, local
from .email_verification import EmailVerification
from .notification_models import bounded_int, parse_time, utc_iso
from .notification_reports import build_report
from .notification_store import NotificationStore
from .notification_worker import NotificationWorker
from .smtp_process import SupervisedSMTP
from .storage import LeadRepository

log = logging.getLogger(__name__)
HEARTBEAT = 'email_worker'
SCHEDULE_PREFIX = 'email_schedule:'
FOOTER = '\n\nRezygnacja z raportów: wpisz /email wylacz w bocie Telegram, w którym je włączono.'


class EmailJobs:
    def __init__(self, store, requests, sender, settings, *, max_leads=200, should_stop=lambda: False):
        self.store, self.requests, self.sender, self.settings = store, requests, sender, settings
        self.bot = BotStore(store.repo)
        self.max_leads = min(bounded_int(max_leads, 1, 2**31 - 1), 200)
        self.should_stop, self.cursor = should_stop, 0
        self.verification = EmailVerification(store, sender)
        self.worker = NotificationWorker(store, {'email': sender})

    def _due(self, endpoint, now):
        last = self.bot.job_time(SCHEDULE_PREFIX + str(endpoint.id))
        if last and last < parse_time(endpoint.updated_at):
            last = None  # Address, mode, activation or consent changed.
        if endpoint.mode == 'natychmiast':
            if last and now < last + timedelta(minutes=self.settings.instant_every_minutes):
                return None
            interval = self.settings.instant_every_minutes * 60
            return str(int(now.timestamp()) // interval)
        time = self.settings.morning_time if endpoint.mode == 'rano' else self.settings.evening_time
        due = at_local_time(now, time)
        if now < due or (last and last >= due):
            return None
        return utc_iso(due)

    def _endpoints(self):
        query = ("SELECT id, chat_id FROM notification_endpoints WHERE channel = 'email'"
                 " AND enabled = 1 AND id > ? ORDER BY id LIMIT 10")
        rows = self.store.repo.connection.execute(query, (self.cursor,)).fetchall()
        if not rows:
            rows = self.store.repo.connection.execute(query, (0,)).fetchall()
        self.cursor = rows[-1]['id'] if rows else 0
        return rows

    def _schedule(self, now):
        remaining = self.max_leads
        for row in self._endpoints():
            if self.should_stop() or remaining <= 0:
                break
            try:
                endpoint = self.store.get_endpoint(row['chat_id'], row['id'])
                slot = self._due(endpoint, now)
                if slot is None:
                    continue
                # Candidates also apply max age, user.nowe_od, filters and current access.
                limit = min(20, remaining)
                candidates = self.store.candidates(endpoint.chat_id, endpoint.id,
                                                   parse_time(endpoint.activated_at or ''), limit=limit)
                remaining -= len(candidates)
                if candidates:
                    parts = build_report(endpoint, candidates, now=now, part_size=20, footer=FOOTER)
                    refs = '\n'.join(f'{ref.id_sprawy}:{ref.revision}' for part in parts for ref in part.leads)
                    digest = hashlib.sha256(refs.encode('utf-8')).hexdigest()
                    key = f'email:{endpoint.mode}:{slot}:v{endpoint.version}:{digest}'
                    self.store.enqueue(endpoint.chat_id, endpoint.id, key, parts, expires_at=now + timedelta(hours=24))
                if len(candidates) < limit:
                    self.bot.set_job_time(SCHEDULE_PREFIX + str(endpoint.id), now)
            except (ValueError, TypeError):
                log.warning('Pominięto planowanie e-mail: odbiorca %s wymaga sprawdzenia', row['id'])

    def run_once(self) -> int:
        if self.should_stop():
            return 0
        now = self.store.repo.now()
        self.bot.set_job_time(HEARTBEAT, now)
        request = self.requests.take()
        if request:
            chat_id, endpoint_id = request
            try:
                if not self.should_stop():
                    self.verification.request(chat_id, endpoint_id)
            finally:
                self.requests.done(chat_id)
        now = self.store.repo.now()
        hour = local(now).hour
        if self.should_stop() or hour >= 22 or hour < 6:
            return 0
        self._schedule(now)
        return 0 if self.should_stop() else self.worker.run_once(limit=1)


class EmailThread(threading.Thread):
    def __init__(self, config, requests, stop):
        super().__init__(name='email-notifications', daemon=True)
        self.config, self.requests, self.stop = config, requests, stop

    def run(self) -> None:
        try:
            with LeadRepository(self.config.storage.db_path,
                                negative_cache_days=self.config.geocoding.negative_cache_days,
                                backup_dir=self.config.storage.backup_dir) as repo:
                store = make_email_store(self.config, repo)
                sender = SupervisedSMTP(self.config.email.smtp, should_stop=self.stop.is_set)
                jobs = EmailJobs(store, self.requests, sender, self.config.bot,
                                 max_leads=self.config.notifications.max_leads_per_run, should_stop=self.stop.is_set)
                while not self.stop.is_set():
                    try:
                        jobs.run_once()
                    except Exception:
                        log.warning('Cykl e-mail nieudany; ponowienie po odczekaniu')
                    self.stop.wait(1)
        except Exception:
            log.error('Wątek e-mail zatrzymał się; sprawdź konfigurację i bazę')


def make_email_store(config, repo):
    return NotificationStore(repo, admins=config.bot.admins, access=config.bot.access,
                             max_age_days=config.notifications.max_age_days)


def prune_notifications(repo, max_age_days: int) -> None:
    """One bounded batch per table nightly; ambiguous outcomes are operator-owned."""
    if repo.connection.in_transaction:
        raise RuntimeError('Retencja wymaga osobnej transakcji')
    now = repo.now()
    max_age_days = bounded_int(max_age_days, 1, 2**31 - 1)
    with repo.transaction():
        conn = repo.connection
        conn.execute('DELETE FROM email_verifications WHERE id IN'
                     ' (SELECT id FROM email_verifications WHERE issued_at < ? ORDER BY issued_at, id LIMIT 2000)',
                     (utc_iso(now - timedelta(days=7)),))
        conn.execute("DELETE FROM notification_outbox WHERE id IN (SELECT id FROM notification_outbox"
                     " WHERE state IN ('accepted', 'delivered', 'read', 'cancelled', 'expired')"
                     " AND updated_at < ? ORDER BY updated_at, id LIMIT 2000)", (utc_iso(now - timedelta(days=30)),))
        conn.execute('DELETE FROM notification_deliveries WHERE rowid IN (SELECT rowid FROM notification_deliveries'
                     ' WHERE processed_at < ? ORDER BY processed_at, rowid LIMIT 2000)',
                     (utc_iso(now - timedelta(days=max(90, max_age_days + 7))),))
        conn.execute('DELETE FROM zadania WHERE nazwa IN (SELECT z.nazwa FROM zadania z'
                     ' WHERE z.nazwa LIKE ? AND NOT EXISTS (SELECT 1 FROM notification_endpoints e'
                     ' WHERE e.id = CAST(substr(z.nazwa, ?) AS INTEGER)) LIMIT 2000)',
                     (SCHEDULE_PREFIX + '%', len(SCHEDULE_PREFIX) + 1))
