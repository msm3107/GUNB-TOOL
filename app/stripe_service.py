"""Test-only Stripe Checkout orchestration. Public sales cannot be enabled here."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

import stripe

from core.checkout_store import CheckoutConflict, CheckoutStore, CheckoutUnavailable
from core.package_selection import PackageFilters


@dataclass(frozen=True)
class CheckoutOffer:
    amount_grosze: int
    interval: str | None = None

    def __post_init__(self):
        if type(self.amount_grosze) is not int or not 1 <= self.amount_grosze <= 100_000_000:
            raise ValueError("Kwota testowa wymaga dodatniej liczby całkowitej groszy")
        if self.interval not in (None, "month", "year"):
            raise ValueError("Niepoprawny okres testowego abonamentu")


def _valid_origin(origin: str) -> bool:
    if not isinstance(origin, str) or not origin.isascii() or any(ord(c) < 33 or ord(c) == 127 or c in "*\\?#" for c in origin):
        return False
    try:
        p = urlsplit(origin)
        return (p.scheme in {"http", "https"} and bool(p.hostname) and p.username is None and p.password is None
                and not p.path and not p.query and not p.fragment and not p.netloc.endswith(":")
                and (p.port is None or 1 <= p.port <= 65535)
                and (p.scheme == "https" or p.hostname in {"localhost", "127.0.0.1", "::1"}))
    except ValueError:
        return False


@dataclass(frozen=True)
class CheckoutSettings:
    test_mode: bool = False
    secret_key: str = field(default="", repr=False)
    origin: str = ""
    package_offer: CheckoutOffer | None = None
    subscription_offer: CheckoutOffer | None = None

    def __post_init__(self):
        if type(self.test_mode) is not bool:
            raise ValueError("Niepoprawny tryb Checkout")
        if not self.test_mode:
            return
        if not isinstance(self.secret_key, str) or not re.fullmatch(r"(?:sk|rk)_test_[A-Za-z0-9]{8,}", self.secret_key):
            raise ValueError("Checkout dopuszcza wyłącznie klucz testowy Stripe")
        if (not _valid_origin(self.origin) or not isinstance(self.package_offer, CheckoutOffer)
                or self.package_offer.interval is not None or not isinstance(self.subscription_offer, CheckoutOffer)
                or self.subscription_offer.interval is None):
            raise ValueError("Niepełna konfiguracja testowego Checkout")

    @classmethod
    def from_env(cls, source):
        mode = source.get("GUNB_WEB_CHECKOUT_MODE", "disabled")
        if mode == "disabled":
            return cls()
        if mode != "test":
            raise ValueError("Checkout dopuszcza tylko disabled lub test")

        def amount(name):
            text = source.get(name, "")
            if not re.fullmatch(r"[0-9]{1,9}", text):
                raise ValueError("Brak poprawnej testowej kwoty w groszach")
            return int(text)

        return cls(True, source.get("GUNB_WEB_STRIPE_SECRET_KEY", ""), source.get("GUNB_WEB_PUBLIC_ORIGIN", ""),
                   CheckoutOffer(amount("GUNB_WEB_STRIPE_TEST_PACKAGE_GROSZE")),
                   CheckoutOffer(amount("GUNB_WEB_STRIPE_TEST_SUBSCRIPTION_GROSZE"), source.get("GUNB_WEB_STRIPE_TEST_INTERVAL", "")))


class CheckoutBusy(CheckoutUnavailable):
    pass


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _session_url(session) -> str:
    try:
        url = session["url"]
        if session["livemode"] is not False or not re.fullmatch(r"cs_test_[A-Za-z0-9]+", session["id"]):
            raise ValueError
        if not isinstance(url, str) or not url.isascii() or len(url) > 4096 or any(ord(c) < 33 or ord(c) == 127 or c == "\\" for c in url):
            raise ValueError
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname != "checkout.stripe.com" or p.username is not None or p.password is not None or p.port not in (None, 443):
            raise ValueError
        return url
    except (KeyError, TypeError, ValueError):
        raise CheckoutUnavailable from None


class CheckoutService:
    def __init__(self, store: CheckoutStore, settings: CheckoutSettings, *, sessions=None, now=None):
        self.store, self.settings = store, settings
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._slots = threading.BoundedSemaphore(4)
        self._rate_lock = threading.Lock()
        self._recent = deque()
        self._sessions = sessions
        if sessions is None and settings.test_mode:
            self._sessions = stripe.StripeClient(settings.secret_key, stripe_version="2024-06-20", max_network_retries=0,
                http_client=stripe.RequestsClient(timeout=5)).checkout.sessions

    def start(self, *, email: str, kind: str, filters: PackageFilters, request_token: str) -> str:
        if not self.settings.test_mode:
            raise CheckoutUnavailable
        email = email.strip().lower()
        if (not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", email)
                or len(email) > 254 or ".." in email or email.startswith(".") or ".@" in email):
            raise ValueError("Podaj poprawny adres e-mail ASCII")
        if kind not in {"jednorazowy", "abonament"} or not isinstance(filters, PackageFilters):
            raise ValueError("Niepoprawny rodzaj paczki lub filtry")
        try:
            token = UUID(request_token)
            if token.version != 4 or str(token) != request_token:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise ValueError("Żądanie wymaga losowego identyfikatora UUIDv4") from None
        if not self._slots.acquire(blocking=False):
            raise CheckoutBusy
        try:
            with self._rate_lock:
                stamp = time.monotonic()
                while self._recent and self._recent[0] <= stamp - 60:
                    self._recent.popleft()
                if len(self._recent) >= 30:
                    raise CheckoutBusy
                self._recent.append(stamp)
            selected = asdict(filters)
            selected["powiaty"] = sorted(selected["powiaty"])
            selected["kategorie"] = sorted(selected["kategorie"])
            filters_json = _json(selected)
            fingerprint = hashlib.sha256(_json([email, kind, selected]).encode()).hexdigest()
            key_hash = hashlib.sha256(request_token.encode()).hexdigest()
            offer = self.settings.package_offer if kind == "jednorazowy" else self.settings.subscription_offer
            offer_snapshot = asdict(offer)
            # Provider idempotency is scoped to an account. A key/account
            # change must not recreate an ambiguous attempt in a new account.
            credential_hash = hashlib.sha256(self.settings.secret_key.encode()).hexdigest()
            offer_snapshot["credential_hash"] = credential_hash
            now = self._now()
            attempt = self.store.reserve(key_hash=key_hash, fingerprint=fingerprint, email=email, filters_json=filters_json,
                kind=kind, offer_json=_json(offer_snapshot), origin=self.settings.origin, now=now)
            if attempt.url:
                return _session_url({"id": attempt.session_id, "livemode": False, "url": attempt.url})
            # Retrying later could present a new Session after provider key
            # retention or violate the minimum remaining Checkout lifetime.
            if int(now.timestamp()) >= attempt.created_at + 1800:
                raise CheckoutConflict
            frozen_offer = json.loads(attempt.offer_json)
            if frozen_offer["credential_hash"] != credential_hash:
                raise CheckoutConflict
            mode = "payment" if attempt.kind == "jednorazowy" else "subscription"
            price = {"currency": "pln", "unit_amount": frozen_offer["amount_grosze"],
                     "product_data": {"name": "Żółta Tablica — test paczki" if mode == "payment" else "Żółta Tablica — test abonamentu"}}
            if mode == "subscription":
                price["recurring"] = {"interval": frozen_offer["interval"]}
            metadata = {"order_id": str(attempt.order_id), "customer_id": str(attempt.customer_id), "checkout_key": attempt.key_hash}
            params = {"mode": mode, "customer_email": attempt.email, "client_reference_id": str(attempt.order_id),
                      "metadata": metadata, "line_items": [{"price_data": price, "quantity": 1}],
                      "payment_method_types": ["card"], "success_url": attempt.origin + "/zamowienie/sukces",
                      "cancel_url": attempt.origin + "/zamowienie", "expires_at": attempt.expires_at,
                      "subscription_data" if mode == "subscription" else "payment_intent_data": {"metadata": metadata}}
            if mode == "payment":
                params["customer_creation"] = "always"
            try:
                session = self._sessions.create(params, {"idempotency_key": "zt-checkout-" + key_hash})
            except stripe.StripeError:
                raise CheckoutUnavailable from None
            url = _session_url(session)
            self.store.complete(key_hash, session["id"], url)
            return url
        finally:
            self._slots.release()
