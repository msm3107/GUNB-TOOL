"""Bounded test-only Customer Portal; identifiers come only from trusted identity."""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from urllib.parse import urlsplit

import stripe

from core.checkout_store import CheckoutUnavailable
from core.subscription_store import SubscriptionStore

from .identity import AuthenticatedCustomer
from .stripe_service import CheckoutBusy, CheckoutSettings


class PortalForbidden(RuntimeError):
    pass


class PortalService:
    def __init__(
        self, store: SubscriptionStore, settings: CheckoutSettings, *, sessions=None
    ):
        self.store, self.settings = store, settings
        self._slots = threading.BoundedSemaphore(4)
        self._rate_lock = threading.Lock()
        self._recent = deque()
        self._sessions = sessions
        if sessions is None and settings.test_mode:
            self._sessions = stripe.StripeClient(
                settings.secret_key,
                stripe_version="2024-06-20",
                max_network_retries=0,
                http_client=stripe.RequestsClient(timeout=5),
            ).billing_portal.sessions

    def start(self, principal: AuthenticatedCustomer) -> str:
        if not self.settings.test_mode:
            raise CheckoutUnavailable
        if not isinstance(principal, AuthenticatedCustomer):
            raise PortalForbidden
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
            customer = self.store.billing_customer(principal.customer_id)
            if not isinstance(customer, str) or not re.fullmatch(
                r"cus_[A-Za-z0-9]{1,251}", customer
            ):
                raise PortalForbidden
            try:
                result = self._sessions.create(
                    {
                        "customer": customer,
                        "return_url": self.settings.origin + "/panel",
                    }
                )
                url = result["url"]
                if (
                    result["livemode"] is not False
                    or result["customer"] != customer
                    or result["object"] != "billing_portal.session"
                    or not re.fullmatch(r"bps_[A-Za-z0-9]+", result["id"])
                    or not isinstance(url, str)
                    or not url.isascii()
                    or len(url) > 4096
                    or any(ord(c) < 33 or ord(c) == 127 or c == "\\" for c in url)
                ):
                    raise ValueError("Invalid portal response")
                p = urlsplit(url)
                if (
                    p.scheme != "https"
                    or p.hostname != "billing.stripe.com"
                    or p.username is not None
                    or p.password is not None
                    or p.port not in (None, 443)
                ):
                    raise ValueError("Invalid portal URL")
            except (stripe.StripeError, ValueError, TypeError, KeyError):
                raise CheckoutUnavailable from None
            if self.store.billing_customer(principal.customer_id) != customer:
                raise PortalForbidden
            return url
        finally:
            self._slots.release()
