"""Feedback from customers about delivered cases; independent of the web layer."""

from __future__ import annotations

import enum
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol


class IssueRepository(Protocol):
    connection: sqlite3.Connection

    def now(self) -> datetime: ...

    def transaction(self) -> AbstractContextManager[None]: ...

MAX_DESCRIPTION_LENGTH = 1000
MAX_REPORTS_PER_HOUR = 20


class IssueType(str, enum.Enum):
    WRONG_PHONE = "zly_telefon"
    DESIGNER_NOT_INVESTOR = "to_nie_inwestor_tylko_projektant"
    ALREADY_BUILT = "inwestycja_juz_stoi"
    OTHER = "inna_uwaga"


@dataclass(frozen=True)
class IssueReport:
    issue_id: int
    customer_id: int
    id_sprawy: str
    issue_type: IssueType
    description: str
    created_at: str


class IssueNotAllowed(PermissionError):
    """The customer has not received a completed delivery containing this case."""


class IssueRateLimitExceeded(RuntimeError):
    """The customer has exhausted the rolling one-hour report allowance."""


def _description(value: str, kind: IssueType) -> str:
    if not isinstance(value, str) or len(value) > MAX_DESCRIPTION_LENGTH:
        raise ValueError("Opis musi być tekstem do 1000 znaków")
    # Store plain text, not trusted HTML. Preserve printable text and normalize controls/whitespace.
    normalized = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    if kind == IssueType.OTHER and not normalized:
        raise ValueError("Inna uwaga wymaga opisu")
    return normalized


def _from_row(row: sqlite3.Row) -> IssueReport:
    return IssueReport(
        row["issue_id"], row["customer_id"], row["id_sprawy"], IssueType(row["typ"]),
        row["opis"], row["zgloszono_utc"],
    )


@contextmanager
def _atomic_report(repository: IssueRepository) -> Iterator[None]:
    # The savepoint also protects callers that catch a failed report inside an outer transaction.
    with repository.transaction():
        connection = repository.connection
        connection.execute("SAVEPOINT issue_report")
        try:
            yield
        except BaseException:
            connection.execute("ROLLBACK TO issue_report")
            connection.execute("RELEASE issue_report")
            raise
        else:
            connection.execute("RELEASE issue_report")


class IssueTracker:
    """Records unverified feedback only for cases delivered to the requesting customer.

    The caller must supply the authenticated customer ID; this core library does
    not authenticate HTTP requests. Report bodies are private plain text and must
    be escaped for their eventual rendering context. Duplicate submissions return
    the original record and do not edit its description or consume the hourly quota.
    """

    def __init__(self, repository: IssueRepository) -> None:
        self._repository = repository

    def report_issue(
        self,
        customer_id: int,
        id_sprawy: str,
        issue_type: IssueType | str,
        description: str = "",
    ) -> IssueReport:
        if type(customer_id) is not int or not 0 < customer_id < 2**63:
            raise ValueError("Niepoprawny identyfikator klienta")
        if not isinstance(id_sprawy, str) or not id_sprawy.strip() or len(id_sprawy) > 512:
            raise ValueError("Niepoprawny identyfikator sprawy")
        try:
            id_sprawy.encode("utf-8")
            kind = IssueType(issue_type)
        except (UnicodeEncodeError, ValueError, TypeError):
            raise ValueError("Niepoprawna sprawa lub typ uwagi") from None
        description = _description(description, kind)
        now = self._repository.now().astimezone(timezone.utc)
        created_at = now.isoformat(timespec="seconds")
        cutoff = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        connection = self._repository.connection

        with _atomic_report(self._repository):
            allowed = connection.execute(
                "SELECT 1 FROM b2b_orders AS orders "
                "JOIN b2b_deliveries AS delivery ON delivery.order_id = orders.order_id "
                "JOIN b2b_delivery_items AS item ON item.delivery_id = delivery.delivery_id "
                "WHERE orders.customer_id = ? AND item.id_sprawy = ? "
                "AND delivery.doreczono_utc IS NOT NULL LIMIT 1",
                (customer_id, id_sprawy),
            ).fetchone()
            if allowed is None:
                raise IssueNotAllowed("Sprawa nie została doręczona temu klientowi")
            existing = connection.execute(
                "SELECT * FROM issue_reports WHERE customer_id = ? AND id_sprawy = ? AND typ = ?",
                (customer_id, id_sprawy, kind.value),
            ).fetchone()
            if existing is not None:
                return _from_row(existing)
            count = connection.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM issue_reports "
                "WHERE customer_id = ? AND zgloszono_utc > ? LIMIT ?)",
                (customer_id, cutoff, MAX_REPORTS_PER_HOUR),
            ).fetchone()[0]
            if count >= MAX_REPORTS_PER_HOUR:
                raise IssueRateLimitExceeded("Limit 20 nowych zgłoszeń na godzinę")
            cursor = connection.execute(
                "INSERT INTO issue_reports (customer_id, id_sprawy, typ, opis, zgloszono_utc) "
                "VALUES (?, ?, ?, ?, ?)",
                (customer_id, id_sprawy, kind.value, description, created_at),
            )
            connection.execute(
                "UPDATE investments SET wymaga_weryfikacji = 1 WHERE id_sprawy = ?", (id_sprawy,)
            )
            return IssueReport(cursor.lastrowid, customer_id, id_sprawy, kind, description, created_at)
