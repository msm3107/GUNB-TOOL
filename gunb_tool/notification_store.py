"""Właściciel odbiorcy i trwały outbox v13. Nie uruchamia nadawców ani harmonogramu."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import hashlib
import re
import sqlite3
from typing import Iterator, Sequence
import unicodedata
import uuid

from .bot_store import BotStore, BotUser, MODES
from .models import Investment
from .notification_models import (CHANNELS, MAX_REPORT_LEADS, Claim, DeliveryResult, LeadRef, NotificationEndpoint, ReportPart,
                                  bounded_int, bounded_text, parse_time, utc_iso)
from .storage import LeadRepository, investment_from_row
from .scoring import HOT

_RESERVING = ("queued", "retry", "sending", "unknown", "failed")
_CHANGING = frozenset({"address", "enabled", "verified_at", "consent_at", "consent_source",
                      "consent_revoked_at", "activated_at"})


class NotificationError(ValueError):
    """Brak własności, gotowości lub miejsca w kolejce; bez adresu/treści w komunikacie."""


def normalize_address(channel: str, address: str) -> str:
    if channel not in CHANNELS or not isinstance(address, str):
        raise ValueError("Niepoprawny kanał lub adres")
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in address):
        raise ValueError("Niedozwolone znaki adresu")
    address = address.strip()
    if channel == "whatsapp":
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", address):
            raise ValueError("Wymagany numer z kodem kraju")
        return address
    if not address.isascii() or len(address) > 254 or address.count("@") != 1:
        raise ValueError("Niepoprawny pojedynczy adres e-mail")
    local, domain = address.split("@")
    if (not 1 <= len(local) <= 64 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
            or local.startswith(".") or local.endswith(".") or ".." in local):
        raise ValueError("Niepoprawny adres e-mail")
    labels = domain.split(".")
    if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
        raise ValueError("Niepoprawna domena e-mail")
    return local + "@" + domain.lower()


def _endpoint(row: sqlite3.Row) -> NotificationEndpoint:
    data = {f.name: row[f.name] for f in fields(NotificationEndpoint)}
    data["enabled"] = bool(data["enabled"])
    return NotificationEndpoint(**data)


class NotificationStore:
    def __init__(self, repo: LeadRepository, *, admins: Sequence[int] = (), access: str = "approval",
                 max_pending_parts: int = 40, max_age_days: int = 30, lease_seconds: int = 120) -> None:
        if access not in ("approval", "open"):
            raise ValueError("Nieznana polityka dostępu")
        self.repo = repo
        self._conn = repo.connection
        self._bot = BotStore(repo)
        self.admins = tuple(bounded_int(chat, -(2**63) + 1, 2**63 - 1) for chat in admins)
        self.access = access
        self.max_pending_parts = bounded_int(max_pending_parts, 1, 100)
        self.max_age_days = bounded_int(max_age_days, 1, 365)
        self.lease_seconds = bounded_int(lease_seconds, 1, 3600)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Każdy zapis jest samodzielny: repo nie cofa osobno zagnieżdżonej operacji."""
        if self._conn.in_transaction:
            raise RuntimeError("Operacja powiadomień wymaga połączenia bez zewnętrznej transakcji")
        with self.repo.transaction():
            yield

    def get_endpoint(self, chat_id: int, endpoint_id: int) -> NotificationEndpoint:
        bounded_int(chat_id, -(2**63) + 1, 2**63 - 1)
        bounded_int(endpoint_id, 1, 2**63 - 1)
        row = self._conn.execute("SELECT * FROM notification_endpoints WHERE chat_id = ? AND id = ?",
                                 (chat_id, endpoint_id)).fetchone()
        if row is None:
            raise NotificationError("Nie znaleziono odbiorcy")
        return _endpoint(row)

    def add_endpoint(self, chat_id: int, channel: str, address: str, *, mode: str = "rano") -> NotificationEndpoint:
        address = normalize_address(channel, address)
        bounded_int(chat_id, -(2**63) + 1, 2**63 - 1)
        if mode not in MODES:
            raise ValueError("Nieznany tryb raportu")
        now = utc_iso(self.repo.now())
        try:
            with self._transaction():
                ident = self._conn.execute(
                    "INSERT INTO notification_endpoints (chat_id, channel, address, mode, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)", (chat_id, channel, address, mode, now, now),
                ).lastrowid
                return self.get_endpoint(chat_id, ident)
        except sqlite3.IntegrityError as exc:
            raise NotificationError("Nie można dodać odbiorcy") from exc

    def _change(self, endpoint: NotificationEndpoint, **values: object) -> NotificationEndpoint:
        if not values or not values.keys() <= _CHANGING:
            raise ValueError("Niepoprawna zmiana odbiorcy")
        now = utc_iso(self.repo.now())
        self._conn.execute(
            "UPDATE notification_endpoints SET " + ", ".join(f"{key} = ?" for key in values)
            + ", version = version + 1, updated_at = ? WHERE id = ?",
            (*values.values(), now, endpoint.id),
        )
        self._conn.execute(
            "UPDATE notification_outbox SET state = 'cancelled', updated_at = ?"
            " WHERE endpoint_id = ? AND state IN ('queued', 'retry')", (now, endpoint.id),
        )
        return self.get_endpoint(endpoint.chat_id, endpoint.id)

    @staticmethod
    def _check_version(endpoint: NotificationEndpoint, version: int) -> None:
        if type(version) is not int or endpoint.version != version:
            raise NotificationError("Wersja odbiorcy się zmieniła")

    def record_verification(self, chat_id: int, endpoint_id: int, *, expected_version: int) -> NotificationEndpoint:
        """Zaufany adapter potwierdził adres dla tej wersji; sam token i limity należą do adaptera."""
        with self._transaction():
            endpoint = self.get_endpoint(chat_id, endpoint_id)
            self._check_version(endpoint, expected_version)
            if endpoint.enabled:
                raise NotificationError("Odbiorca jest już aktywny")
            now = utc_iso(self.repo.now())
            self._conn.execute("UPDATE notification_endpoints SET verified_at = ?, updated_at = ? WHERE id = ?",
                               (now, now, endpoint.id))
            return self.get_endpoint(chat_id, endpoint_id)

    def record_consent(self, chat_id: int, endpoint_id: int, *, expected_version: int,
                       source: str) -> NotificationEndpoint:
        source = bounded_text(source, 200, single_line=True)
        with self._transaction():
            endpoint = self.get_endpoint(chat_id, endpoint_id)
            self._check_version(endpoint, expected_version)
            return self._change(endpoint, enabled=0, consent_at=utc_iso(self.repo.now()),
                                consent_source=source, consent_revoked_at=None, activated_at=None)

    def set_enabled(self, chat_id: int, endpoint_id: int, enabled: bool, *, expected_version: int) -> NotificationEndpoint:
        if type(enabled) is not bool:
            raise ValueError("Wymagana wartość logiczna")
        with self._transaction():
            endpoint = self.get_endpoint(chat_id, endpoint_id)
            self._check_version(endpoint, expected_version)
            if enabled and not self._has_proofs(endpoint):
                raise NotificationError("Brak aktualnej weryfikacji lub zgody")
            return self._change(endpoint, enabled=int(enabled), activated_at=utc_iso(self.repo.now()) if enabled else None)

    def change_address(self, chat_id: int, endpoint_id: int, address: str) -> NotificationEndpoint:
        with self._transaction():
            endpoint = self.get_endpoint(chat_id, endpoint_id)
            address = normalize_address(endpoint.channel, address)
            if address == endpoint.address:
                return endpoint
            try:
                return self._change(endpoint, address=address, enabled=0, verified_at=None, consent_at=None,
                                    consent_source=None, consent_revoked_at=utc_iso(self.repo.now()), activated_at=None)
            except sqlite3.IntegrityError as exc:
                raise NotificationError("Nie można zmienić adresu") from exc

    def revoke_consent(self, chat_id: int, endpoint_id: int) -> NotificationEndpoint:
        with self._transaction():
            endpoint = self.get_endpoint(chat_id, endpoint_id)
            return self._change(endpoint, enabled=0, consent_revoked_at=utc_iso(self.repo.now()), activated_at=None)

    def delete_endpoint(self, chat_id: int, endpoint_id: int) -> None:
        with self._transaction():
            self.get_endpoint(chat_id, endpoint_id)
            self._conn.execute("DELETE FROM notification_endpoints WHERE id = ? AND chat_id = ?", (endpoint_id, chat_id))

    def _has_proofs(self, endpoint: NotificationEndpoint) -> bool:
        try:
            bounded_text(endpoint.consent_source or "", 200, single_line=True)
            return (normalize_address(endpoint.channel, endpoint.address) == endpoint.address
                    and endpoint.consent_revoked_at is None
                    and parse_time(endpoint.verified_at or "") <= self.repo.now()
                    and parse_time(endpoint.consent_at or "") <= self.repo.now())
        except (ValueError, TypeError):
            return False

    def _eligible_user(self, endpoint: NotificationEndpoint) -> BotUser | None:
        if not endpoint.enabled or not endpoint.activated_at or not self._has_proofs(endpoint):
            return None
        try:
            if parse_time(endpoint.activated_at) > self.repo.now():
                return None
        except (ValueError, TypeError):
            return None
        user = self._bot.get_user(endpoint.chat_id)
        if user is None or user.status != "aktywny" or user.wstrzymane:
            return None
        if self.access == "open" or user.chat_id in self.admins or user.bez_limitu:
            return user
        try:
            return user if user.is_active and parse_time(user.subscription_ends or "") > self.repo.now() else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _wanted(user: BotUser, inv: Investment) -> bool:
        return not inv.is_noise and user.filtry.matches(inv) and (not user.tylko_hot or inv.priorytet == HOT)

    def _reservations(self, endpoint_id: int) -> tuple[int, set[LeadRef]]:
        marks = ",".join("?" for _ in _RESERVING)
        rows = self._conn.execute(
            f"SELECT payload FROM notification_outbox WHERE endpoint_id = ? AND state IN ({marks}) LIMIT ?",
            (endpoint_id, *_RESERVING, self.max_pending_parts + 1),
        ).fetchall()
        if len(rows) > self.max_pending_parts:
            raise NotificationError("Przekroczony limit kolejki odbiorcy")
        refs: set[LeadRef] = set()
        try:
            for row in rows:
                refs.update(ReportPart.from_json(row["payload"]).leads)
        except ValueError as exc:
            raise NotificationError("Kolejka odbiorcy wymaga sprawdzenia") from exc
        return len(rows), refs

    def candidates(self, chat_id: int, endpoint_id: int, since: datetime, *, limit: int = 20) -> list[Investment]:
        bounded_int(limit, 1, MAX_REPORT_LEADS)
        utc_iso(since)
        endpoint = self.get_endpoint(chat_id, endpoint_id)
        user = self._eligible_user(endpoint)
        if user is None:
            return []
        _, reserved = self._reservations(endpoint.id)
        since = max(since.astimezone(timezone.utc), self.repo.now() - timedelta(days=self.max_age_days), parse_time(user.nowe_od))
        rows = self._conn.execute(
            "SELECT i.* FROM investments i WHERE i.is_noise = 0 AND i.status_zmieniony > ?"
            " AND NOT EXISTS (SELECT 1 FROM user_leads u WHERE u.chat_id = ? AND u.id_sprawy = i.id_sprawy AND u.ukryty = 1)"
            " AND NOT EXISTS (SELECT 1 FROM notification_deliveries d"
            " WHERE d.endpoint_id = ? AND d.id_sprawy = i.id_sprawy AND d.revision = i.status_zmieniony)"
            " ORDER BY i.status_zmieniony, i.data_aktualizacji, i.nr", (utc_iso(since), chat_id, endpoint.id),
        )
        matches = []
        try:
            for row in rows:
                inv = investment_from_row(row)
                if LeadRef(inv.id_sprawy, inv.status_zmieniony or "") not in reserved and self._wanted(user, inv):
                    matches.append(inv)
                    if len(matches) == limit:
                        break
        finally:
            rows.close()
        return matches

    def _valid_refs(self, user: BotUser, refs: Sequence[LeadRef]) -> bool:
        marks = ",".join("?" for _ in refs)
        rows = self._conn.execute(
            f"SELECT i.* FROM investments i WHERE i.id_sprawy IN ({marks})"
            " AND NOT EXISTS (SELECT 1 FROM user_leads u WHERE u.chat_id = ? AND u.id_sprawy = i.id_sprawy AND u.ukryty = 1)",
            (*[ref.id_sprawy for ref in refs], user.chat_id),
        ).fetchall()
        investments = {row["id_sprawy"]: investment_from_row(row) for row in rows}
        return all(ref.id_sprawy in investments and investments[ref.id_sprawy].status_zmieniony == ref.revision
                   and self._wanted(user, investments[ref.id_sprawy]) for ref in refs)

    def enqueue(self, chat_id: int, endpoint_id: int, event_key: str, parts: Sequence[ReportPart], *,
                expires_at: datetime) -> tuple[int, ...]:
        bounded_text(event_key, 256, single_line=True)
        if len(parts) > MAX_REPORT_LEADS or any(not isinstance(part, ReportPart) for part in parts):
            raise ValueError("Niepoprawne części raportu")
        refs = tuple(ref for part in parts for ref in part.leads)
        if len(refs) > MAX_REPORT_LEADS or len(set(refs)) != len(refs):
            raise ValueError("Niepoprawna partia rewizji")
        with self._transaction():
            endpoint = self.get_endpoint(chat_id, endpoint_id)
            if any(part.chat_id != chat_id for part in parts):
                raise NotificationError("Niezgodny właściciel raportu")
            existing = self._conn.execute(
                "SELECT id FROM notification_outbox WHERE endpoint_id = ? AND event_key = ? ORDER BY part",
                (endpoint_id, event_key),
            ).fetchall()
            if existing:
                return tuple(row["id"] for row in existing)
            if not parts:
                return ()
            utc_iso(expires_at)
            now = self.repo.now()
            if not now < expires_at <= now + timedelta(hours=24):
                raise ValueError("Niepoprawny termin ważności raportu")
            user = self._eligible_user(endpoint)
            if user is None or any(part.endpoint_version != endpoint.version for part in parts) or not self._valid_refs(user, refs):
                raise NotificationError("Odbiorca lub raport nie jest gotowy")
            count, reserved = self._reservations(endpoint_id)
            if reserved.intersection(refs):
                return ()
            if self._has_history(endpoint_id, refs):
                return ()
            if count + len(parts) > self.max_pending_parts:
                raise NotificationError("Brak miejsca w kolejce odbiorcy")
            stamp = utc_iso(now)
            ids = []
            for number, part in enumerate(parts):
                ids.append(self._conn.execute(
                    "INSERT INTO notification_outbox"
                    " (endpoint_id, event_key, part, payload, next_attempt_at, expires_at, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (endpoint_id, event_key, number, part.to_json(), stamp, utc_iso(expires_at), stamp, stamp),
                ).lastrowid)
            return tuple(ids)

    def _has_history(self, endpoint_id: int, refs: Sequence[LeadRef]) -> bool:
        if not refs:
            return False
        # Każda gałąź korzysta z istniejącego PK; jedno zapytanie zamiast N odczytów.
        clauses = " OR ".join("(endpoint_id = ? AND id_sprawy = ? AND revision = ?)" for _ in refs)
        params = tuple(value for ref in refs for value in (endpoint_id, ref.id_sprawy, ref.revision))
        return self._conn.execute(f"SELECT 1 FROM notification_deliveries WHERE {clauses} LIMIT 1", params).fetchone() is not None

    def _quarantine(self, endpoint_id: int) -> None:
        """Zatrzymanie jednego kanału, bez zmiany konta/Telegrama i bez cofania zgody."""
        stamp = utc_iso(self.repo.now())
        self._conn.execute(
            "UPDATE notification_endpoints SET enabled = 0, activated_at = NULL, version = version + 1, updated_at = ?"
            " WHERE id = ? AND enabled = 1", (stamp, endpoint_id),
        )
        self._conn.execute(
            "UPDATE notification_outbox SET state = 'cancelled', updated_at = ?"
            " WHERE endpoint_id = ? AND state IN ('queued', 'retry')", (stamp, endpoint_id),
        )

    def _mark(self, ident: int, outcome: str, *, keep_claim: bool = False) -> None:
        clear = "" if keep_claim else ", claim_owner = NULL, claimed_at = NULL"
        self._conn.execute("UPDATE notification_outbox SET state = ?, updated_at = ?" + clear + " WHERE id = ?",
                           (outcome, utc_iso(self.repo.now()), ident))

    def _recover_claims(self, channels: Sequence[str]) -> None:
        marks = ",".join("?" for _ in channels)
        rows = self._conn.execute(
            "SELECT o.id, o.endpoint_id, o.claimed_at, o.claim_owner FROM notification_outbox o"
            " JOIN notification_endpoints e ON e.id = o.endpoint_id"
            f" WHERE o.state = 'sending' AND e.channel IN ({marks}) ORDER BY o.claimed_at, o.id LIMIT 100", tuple(channels),
        ).fetchall()
        now = self.repo.now()
        for row in rows:
            try:
                claimed = parse_time(row["claimed_at"] or "")
                abandoned = not row["claim_owner"] or claimed > now or now >= claimed + timedelta(seconds=self.lease_seconds)
            except (ValueError, TypeError):
                abandoned = True
            if abandoned:
                # Nie przejmujemy ponownie: dostawca mógł już przyjąć wiadomość.
                self._mark(row["id"], "unknown", keep_claim=True)
                self._quarantine(row["endpoint_id"])

    def claim(self, channels: Sequence[str]) -> Claim | None:
        channels = tuple(dict.fromkeys(channels))
        if any(channel not in CHANNELS for channel in channels):
            raise ValueError("Nieznany kanał workera")
        if not channels:
            return None
        with self._transaction():
            self._recover_claims(channels)
            marks = ",".join("?" for _ in channels)
            stamp = utc_iso(self.repo.now())
            row = self._conn.execute(
                "SELECT o.* FROM notification_outbox o JOIN notification_endpoints e ON e.id = o.endpoint_id"
                f" WHERE o.state IN ('queued', 'retry') AND o.next_attempt_at <= ? AND e.channel IN ({marks})"
                " AND NOT EXISTS (SELECT 1 FROM notification_outbox busy"
                " WHERE busy.endpoint_id = o.endpoint_id AND busy.state = 'sending')"
                " ORDER BY o.next_attempt_at, o.id LIMIT 1", (stamp, *channels),
            ).fetchone()
            if row is None:
                return None
            owner = uuid.uuid4().hex
            self._conn.execute(
                "UPDATE notification_outbox SET state = 'sending', claimed_at = ?, claim_owner = ?,"
                " attempts = attempts + 1, updated_at = ? WHERE id = ?", (stamp, owner, stamp, row["id"]),
            )
            return Claim(row["id"], row["endpoint_id"], row["event_key"], row["part"], owner, stamp, row["attempts"] + 1)

    def _claimed_row(self, claim: Claim) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM notification_outbox WHERE id = ? AND endpoint_id = ? AND claim_owner = ? AND claimed_at = ?"
            " AND state IN ('sending', 'unknown')",
            (claim.id, claim.endpoint_id, claim.claim_owner, claim.claimed_at),
        ).fetchone()

    def prepare_send(self, claim: Claim) -> tuple[NotificationEndpoint, ReportPart] | None:
        """Ostatnia kontrola; zwracamy niemutowalnego odbiorcę zamiast czytać nowy adres po jej zakończeniu."""
        with self._transaction():
            row = self._claimed_row(claim)
            if row is None or row["state"] != "sending":
                return None
            now = self.repo.now()
            if now >= parse_time(row["claimed_at"]) + timedelta(seconds=self.lease_seconds):
                self._mark(claim.id, "unknown", keep_claim=True)
                self._quarantine(claim.endpoint_id)
                return None
            try:
                if parse_time(row["expires_at"] or "") <= now:
                    self._mark(claim.id, "expired")
                    return None
                part = ReportPart.from_json(row["payload"])
                endpoint = _endpoint(self._conn.execute(
                    "SELECT * FROM notification_endpoints WHERE id = ?", (claim.endpoint_id,),
                ).fetchone())
                user = self._eligible_user(endpoint)
                valid = (user is not None and part.chat_id == endpoint.chat_id and part.endpoint_version == endpoint.version
                         and self._valid_refs(user, part.leads) and not self._has_history(endpoint.id, part.leads))
            except (ValueError, TypeError):
                self._mark(claim.id, "failed")
                self._quarantine(claim.endpoint_id)
                return None
            if not valid:
                self._mark(claim.id, "cancelled")
                return None
            return endpoint, part

    def complete(self, claim: Claim, result: DeliveryResult, *, max_attempts: int = 5) -> bool:
        bounded_int(max_attempts, 1, 10)
        if not isinstance(result, DeliveryResult):
            raise ValueError("Niepoprawny wynik nadawcy")
        with self._transaction():
            row = self._claimed_row(claim)
            if row is None:
                return False
            outcome = result.outcome
            if outcome == "retry" and row["attempts"] >= max_attempts:
                outcome = "failed"
            if outcome in ("accepted", "delivered", "read"):
                try:
                    part = ReportPart.from_json(row["payload"])
                except ValueError:
                    self._mark(claim.id, "unknown", keep_claim=True)
                    self._quarantine(claim.endpoint_id)
                    return False
                owner = self._conn.execute("SELECT chat_id FROM notification_endpoints WHERE id = ?", (claim.endpoint_id,)).fetchone()
                if owner is None or owner["chat_id"] != part.chat_id:
                    self._mark(claim.id, "unknown", keep_claim=True)
                    self._quarantine(claim.endpoint_id)
                    return False
                stamp = utc_iso(self.repo.now())
                self._conn.executemany(
                    "INSERT INTO notification_deliveries (endpoint_id, id_sprawy, revision, kind, outcome, processed_at, outbox_id)"
                    " SELECT ?, id_sprawy, ?, 'report', ?, ?, ? FROM investments WHERE id_sprawy = ?"
                    " ON CONFLICT (endpoint_id, id_sprawy, revision) DO UPDATE SET"
                    " outcome = CASE WHEN notification_deliveries.outcome = 'read' THEN 'read'"
                    " WHEN notification_deliveries.outcome = 'delivered' AND excluded.outcome = 'accepted' THEN 'delivered'"
                    " ELSE excluded.outcome END, processed_at = excluded.processed_at, outbox_id = excluded.outbox_id",
                    [(claim.endpoint_id, ref.revision, outcome, stamp, claim.id, ref.id_sprawy) for ref in part.leads],
                )
                self._conn.execute("UPDATE notification_outbox SET provider_message_id = ? WHERE id = ?",
                                   (result.provider_message_id, claim.id))
                self._mark(claim.id, outcome)
            elif outcome == "retry":
                jitter = int(hashlib.sha256(f"{claim.id}:{row['attempts']}".encode()).hexdigest()[:4], 16) % 16
                delay = max(min(3600, 60 * 2 ** min(row["attempts"] - 1, 10)) + jitter, result.retry_after_seconds or 0)
                self._conn.execute(
                    "UPDATE notification_outbox SET state = 'retry', next_attempt_at = ?, updated_at = ?,"
                    " claim_owner = NULL, claimed_at = NULL WHERE id = ?",
                    (utc_iso(self.repo.now() + timedelta(seconds=delay)), utc_iso(self.repo.now()), claim.id),
                )
            else:
                self._mark(claim.id, outcome, keep_claim=outcome == "unknown")
                self._quarantine(claim.endpoint_id)
            return True
