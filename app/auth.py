"""Explicit test-only Magic Link authentication; no secrets in URLs sent to HTTP."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Request

from core.auth_store import AuthRejected, AuthStore, BillingLinkMissing
from core.checkout_store import CheckoutUnavailable
from core.email_sender import SMTPSettings, normalize_address
from core.smtp_process import SupervisedSMTP

from .identity import AuthenticatedCustomer
from .navigation import safe_return_path
from .stripe_service import CheckoutBusy, _valid_origin

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthSettings:
    enabled: bool = False
    secret_key: str = field(default="", repr=False)
    origin: str = ""
    smtp: SMTPSettings | None = field(default=None, repr=False)

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Invalid auth mode")
        if self.enabled and (
            not re.fullmatch(r"[0-9a-f]{64}", self.secret_key)
            or not _valid_origin(self.origin)
            or not isinstance(self.smtp, SMTPSettings)
        ):
            raise ValueError(
                "Auth requires test configuration, strong secret, fixed origin and TLS SMTP"
            )

    @classmethod
    def from_env(cls, source):
        mode = source.get("GUNB_WEB_AUTH_MODE", "disabled")
        if mode == "disabled":
            return cls()
        if mode != "test":
            raise ValueError("Auth supports disabled or test only")
        try:
            smtp = SMTPSettings(
                source.get("GUNB_WEB_AUTH_SMTP_HOST", ""),
                int(source.get("GUNB_WEB_AUTH_SMTP_PORT", "587")),
                source.get("GUNB_WEB_AUTH_SMTP_FROM", ""),
                source.get("GUNB_WEB_AUTH_SMTP_TLS", "starttls"),
                source.get("GUNB_WEB_AUTH_SMTP_USERNAME", ""),
                source.get("GUNB_WEB_AUTH_SMTP_PASSWORD", ""),
            )
        except (ValueError, TypeError):
            raise ValueError("Invalid auth SMTP configuration") from None
        return cls(
            True,
            source.get("GUNB_WEB_AUTH_SECRET", ""),
            source.get("GUNB_WEB_AUTH_ORIGIN", ""),
            smtp,
        )


class TokenCodec:
    def __init__(self, secret_key: str):
        self._key = bytes.fromhex(secret_key)

    def issue(self, purpose: str, now: datetime) -> str:
        value = f"v1.{int(now.timestamp())}.{secrets.token_urlsafe(32)}"
        signature = hmac.new(
            self._key, f"zt:{purpose}:{value}".encode(), hashlib.sha256
        ).hexdigest()
        return value + "." + signature

    def valid(self, token: str, purpose: str, now: datetime) -> bool:
        if not isinstance(token, str) or not re.fullmatch(
            r"v1\.[0-9]{1,12}\.[A-Za-z0-9_-]{43}\.[0-9a-f]{64}", token
        ):
            return False
        value, signature = token.rsplit(".", 1)
        stamp = int(value.split(".")[1])
        clock = int(now.timestamp())
        ttl = 900 if purpose == "magic" else 28800
        expected = hmac.new(
            self._key, f"zt:{purpose}:{value}".encode(), hashlib.sha256
        ).hexdigest()
        return (
            stamp <= clock + 30
            and clock < stamp + ttl
            and hmac.compare_digest(signature, expected)
        )


@dataclass(frozen=True)
class MagicMail:
    recipient: str = field(repr=False)
    token: str = field(repr=False)
    token_hash: str
    return_to: str = field(default="/panel", repr=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def magic_digest(token: str, return_to: str) -> str:
    # Preserve existing /panel links, bind every other destination to its row.
    return digest(token if return_to == "/panel" else "zt-return:" + return_to + "\n" + token)


def valid_browser(value) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None
    )


class AuthService:
    def __init__(
        self, store: AuthStore, settings: AuthSettings, *, sender=None, now=None
    ):
        self.store, self.settings = store, settings
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._codec = TokenCodec(settings.secret_key) if settings.enabled else None
        self._sender = sender or (
            SupervisedSMTP(settings.smtp, timeout_seconds=15)
            if settings.enabled
            else None
        )
        self._request_slots = threading.BoundedSemaphore(4)
        self._send_slots = threading.BoundedSemaphore(2)
        self._billing_slots = threading.BoundedSemaphore(2)
        self._billing_lock = threading.Lock()
        self._billing_recent = deque()
        self.secure = settings.origin.startswith("https://")
        self.session_cookie = "__Host-zt_session" if self.secure else "zt_session"
        self.browser_cookie = (
            "__Host-zt_magic_browser" if self.secure else "zt_magic_browser"
        )

    def prepare(self, email: str, browser: str, peer: str, *, return_to: str = "/panel") -> MagicMail | None:
        return_to = safe_return_path(return_to)
        if not self.settings.enabled:
            raise CheckoutUnavailable
        if not valid_browser(browser):
            raise AuthRejected
        email = normalize_address("email", email.strip().lower())
        local, domain = email.split("@")
        if (
            "." not in domain
            or ".." in email
            or local.startswith(".")
            or local.endswith(".")
        ):
            raise ValueError("Invalid email")
        if not self._request_slots.acquire(blocking=False):
            return None
        try:
            now = self._now()
            token = self._codec.issue("magic", now)

            # HMAC, rather than a raw email/IP hash, resists dictionary reversal.
            def keyed(label, value):
                return hmac.new(
                    bytes.fromhex(self.settings.secret_key),
                    f"{label}:{value}".encode(),
                    hashlib.sha256,
                ).hexdigest()

            issued = self.store.issue_link(
                email=email,
                token_hash=magic_digest(token, return_to),
                browser_hash=digest(browser),
                address_hash=keyed("address", email),
                ip_hash=keyed("peer", peer),
                now=now,
            )
            return MagicMail(email, token, magic_digest(token, return_to), return_to) if issued else None
        finally:
            self._request_slots.release()

    def deliver(self, mail: MagicMail) -> None:
        outcome = "retry"
        if self._send_slots.acquire(blocking=False):
            try:
                result = self._sender.send_email(
                    mail.recipient,
                    "Żółta Tablica — link logowania",
                    "Zlecono logowanie do Żółtej Tablicy. Otwórz link w tej samej przeglądarce, w której go zamówiono, "
                    "i wybierz Potwierdź logowanie. Link jest jednorazowy i ważny 15 minut.\n\n"
                    + self.settings.origin
                    + "/auth/verify"
                    + ("?" + urlencode({"next": mail.return_to}) if mail.return_to != "/panel" else "")
                    + "#"
                    + mail.token
                    + "\n\nJeśli nie zlecano logowania, zignoruj wiadomość. Nie przekazuj linku innym osobom.",
                    idempotency_key="zt-login-" + mail.token_hash,
                )
                outcome = (
                    result.outcome
                    if result.outcome in {"accepted", "retry", "failed", "unknown"}
                    else "unknown"
                )
            except Exception:  # noqa: BLE001 - SMTP failure must not log a capability or escape after ACK
                outcome = "unknown"
            finally:
                self._send_slots.release()
        try:
            self.store.mark_delivery(mail.token_hash, outcome)
        except CheckoutUnavailable:
            log.warning("Auth mail receipt could not be persisted")

    def verify(self, token: str, browser: str, old_session: str | None = None, *, return_to: str = "/panel") -> str:
        return_to = safe_return_path(return_to)
        if not self.settings.enabled:
            raise CheckoutUnavailable
        now = self._now()
        if not valid_browser(browser) or not self._codec.valid(token, "magic", now):
            raise AuthRejected
        session = self._codec.issue("session", now)
        old_hash = (
            digest(old_session)
            if self._codec.valid(old_session, "session", now)
            else None
        )
        self.store.consume_link(
            token_hash=magic_digest(token, return_to),
            browser_hash=digest(browser),
            session_hash=digest(session),
            old_session_hash=old_hash,
            now=now,
        )
        return session

    def resolve(self, request: Request) -> AuthenticatedCustomer | None:
        if not self.settings.enabled:
            return None
        token = request.cookies.get(self.session_cookie)
        now = self._now()
        if not self._codec.valid(token, "session", now):
            return None
        customer = self.store.session_customer(digest(token), now=now)
        return AuthenticatedCustomer(customer, digest(token)) if customer is not None else None

    def logout(self, request: Request) -> None:
        token = request.cookies.get(self.session_cookie)
        if self.settings.enabled and self._codec.valid(token, "session", self._now()):
            self.store.revoke_session(digest(token))

    def bind_billing(
        self, principal: AuthenticatedCustomer, request_token: str, subscriptions
    ) -> None:
        if not self.settings.enabled or not subscriptions.settings.test_mode:
            raise CheckoutUnavailable
        token = UUID(request_token)
        if token.version != 4 or str(token) != request_token:
            raise ValueError("Invalid order capability")
        self._bind_billing(principal, subscriptions, key_hash=digest(request_token))

    def bind_order(self, principal: AuthenticatedCustomer, order_id: int, subscriptions) -> None:
        if not self.settings.enabled or not subscriptions.settings.test_mode:
            raise CheckoutUnavailable
        if type(order_id) is not int or not 1 <= order_id < 2**63:
            raise ValueError("Invalid order")
        self._bind_billing(principal, subscriptions, order_id=order_id)

    def _bind_billing(self, principal, subscriptions, *, key_hash=None, order_id=None):
        if not self._billing_slots.acquire(blocking=False):
            raise CheckoutBusy
        try:
            with self._billing_lock:
                stamp = time.monotonic()
                while self._billing_recent and self._billing_recent[0] <= stamp - 60:
                    self._billing_recent.popleft()
                if len(self._billing_recent) >= 10:
                    raise CheckoutBusy
                self._billing_recent.append(stamp)
            try:
                notice = self.store.claim_billing(
                    principal.customer_id, key_hash, order_id=order_id,
                    session_hash=principal.session_hash, now=self._now(),
                )
            except BillingLinkMissing as missing:
                restored = subscriptions.recover_checkout(missing.attempt)
                notice = self.store.claim_billing(
                    principal.customer_id, key_hash, recovered=restored, order_id=order_id,
                    session_hash=principal.session_hash, now=self._now(),
                )
            subscriptions.reconcile(notice, session_hash=principal.session_hash)
            self.store.claim_billing(
                principal.customer_id, key_hash, order_id=order_id,
                session_hash=principal.session_hash, now=self._now(),
            )
        finally:
            self._billing_slots.release()
