"""Niemutowalne dane nowych kanałów; format kolejki niezależny od Telegrama."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import unicodedata

CHANNELS = ("email", "whatsapp")
MAX_REPORT_LEADS = 20
MAX_PAYLOAD_BYTES = 65536
MAX_BODY_BYTES = 32768


def utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Wymagany czas ze strefą czasową")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    utc_iso(result)
    return result.astimezone(timezone.utc)


def bounded_text(value: str, size: int, *, single_line: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Wymagany niepusty tekst")
    if len(value.encode("utf-8")) > size:
        raise ValueError("Tekst przekracza limit")
    if single_line and any(unicodedata.category(ch) in ("Cc", "Cf") for ch in value):
        raise ValueError("Niedozwolone znaki sterujące")
    return value


def bounded_int(value: int, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Liczba poza dozwolonym zakresem")
    return value


@dataclass(frozen=True)
class NotificationEndpoint:
    id: int
    chat_id: int
    channel: str
    address: str = field(repr=False)
    enabled: bool
    mode: str
    verified_at: str | None
    consent_at: str | None
    consent_source: str | None
    consent_revoked_at: str | None
    activated_at: str | None
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class LeadRef:
    id_sprawy: str
    revision: str

    def __post_init__(self) -> None:
        bounded_text(self.id_sprawy, 256, single_line=True)
        bounded_text(self.revision, 128, single_line=True)
        parse_time(self.revision)


@dataclass(frozen=True)
class ReportPart:
    chat_id: int
    endpoint_version: int
    title: str
    body: str = field(repr=False)
    leads: tuple[LeadRef, ...]

    def __post_init__(self) -> None:
        bounded_int(self.chat_id, -(2**63) + 1, 2**63 - 1)
        if self.chat_id == 0:
            raise ValueError("Brak właściciela")
        bounded_int(self.endpoint_version, 1, 2**31 - 1)
        bounded_text(self.title, 120, single_line=True)
        bounded_text(self.body, MAX_BODY_BYTES)
        if not isinstance(self.leads, tuple) or not 1 <= len(self.leads) <= MAX_REPORT_LEADS:
            raise ValueError("Niepoprawna liczba inwestycji")
        if any(not isinstance(ref, LeadRef) for ref in self.leads) or len(set(self.leads)) != len(self.leads):
            raise ValueError("Niepoprawne lub powtórzone rewizje")

    def to_json(self) -> str:
        result = json.dumps({
            "version": 1, "chat_id": self.chat_id, "endpoint_version": self.endpoint_version,
            "title": self.title, "body": self.body,
            "leads": [{"id_sprawy": ref.id_sprawy, "revision": ref.revision} for ref in self.leads],
        }, ensure_ascii=False, separators=(",", ":"))
        bounded_text(result, MAX_PAYLOAD_BYTES)
        return result

    @classmethod
    def from_json(cls, payload: str) -> ReportPart:
        try:
            bounded_text(payload, MAX_PAYLOAD_BYTES)
            raw = json.loads(payload)
            keys = {"version", "chat_id", "endpoint_version", "title", "body", "leads"}
            if not isinstance(raw, dict) or set(raw) != keys or type(raw["version"]) is not int or raw["version"] != 1:
                raise ValueError("Nieobsługiwany format")
            if not isinstance(raw["leads"], list) or not 1 <= len(raw["leads"]) <= MAX_REPORT_LEADS:
                raise ValueError("Niepoprawne rewizje")
            refs = []
            for ref in raw["leads"]:
                if not isinstance(ref, dict) or set(ref) != {"id_sprawy", "revision"}:
                    raise ValueError("Niepoprawna rewizja")
                refs.append(LeadRef(**ref))
            return cls(raw["chat_id"], raw["endpoint_version"], raw["title"], raw["body"], tuple(refs))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
            raise ValueError("Niepoprawny snapshot raportu") from exc


@dataclass(frozen=True)
class Claim:
    id: int
    endpoint_id: int
    event_key: str
    part: int
    claim_owner: str
    claimed_at: str
    attempts: int

    @property
    def idempotency_key(self) -> str:
        raw = json.dumps([self.endpoint_id, self.event_key, self.part], separators=(",", ":"))
        return "gunb-outbox:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeliveryResult:
    outcome: str
    provider_message_id: str | None = field(default=None, repr=False)
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.outcome not in ("accepted", "delivered", "read", "retry", "failed", "unknown"):
            raise ValueError("Nieznany wynik nadawcy")
        if self.provider_message_id is not None:
            bounded_text(self.provider_message_id, 256, single_line=True)
        if self.retry_after_seconds is not None:
            bounded_int(self.retry_after_seconds, 0, 86400)
            if self.outcome != "retry":
                raise ValueError("Retry-After wymaga wyniku retry")
