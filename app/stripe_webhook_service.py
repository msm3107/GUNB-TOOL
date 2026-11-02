"""Verify test snapshot events before entering the framework-free payment store."""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone

import stripe

from core.checkout_store import CheckoutUnavailable
from core.payment_store import PaidCheckout, PaymentStore

from .subscription_service import SubscriptionService, parse_notice


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("invalid JSON constant")


def _identifier(value, pattern: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 255
        or not re.fullmatch(pattern, value)
    ):
        raise ValueError("invalid identifier")
    return value


class StripeWebhookService:
    def __init__(self, store: PaymentStore, secret: str, *, enabled: bool, subscription_service: SubscriptionService | None = None):
        self.store, self._secret, self.enabled = store, secret, enabled and bool(secret)
        self._slots = threading.BoundedSemaphore(4)
        self._subscription_service = subscription_service

    def receive(self, raw: bytes, signature: str) -> int | None:
        if not self.enabled or not self._slots.acquire(blocking=False):
            raise CheckoutUnavailable
        try:
            try:
                # The SDK consumes the exact bytes, never a serialized parsed object.
                stripe.Webhook.construct_event(
                    raw, signature, self._secret, tolerance=300
                )
                timestamps = [
                    part[2:] for part in signature.split(",") if part.startswith("t=")
                ]
                if len(timestamps) != 1 or abs(int(timestamps[0]) - time.time()) > 300:
                    raise ValueError("signature timestamp")
                event = json.loads(
                    raw,
                    object_pairs_hook=_unique_object,
                    parse_constant=_invalid_constant,
                )
                if (
                    not isinstance(event, dict)
                    or event.get("object") != "event"
                    or event.get("livemode") is not False
                    or event.get("account") is not None
                    or event.get("context") is not None
                    or event.get("api_version") != "2024-06-20"
                ):
                    raise ValueError("unsupported event envelope")
                event_id = _identifier(event.get("id"), r"evt_[A-Za-z0-9]+")
                event_type = _identifier(event.get("type"), r"[a-z][a-z0-9_.]+")
                completion = None
                subscription_notice = None
                if event_type in {'customer.subscription.updated','customer.subscription.deleted'}:
                    subscription_notice = parse_notice(event['data']['object'],deleted=event_type.endswith('.deleted'))
                if event_type == "checkout.session.completed":
                    session = event["data"]["object"]
                    if (
                        not isinstance(session, dict)
                        or session.get("object") != "checkout.session"
                        or session.get("livemode") is not False
                        or session.get("status") != "complete"
                    ):
                        raise ValueError("invalid session")
                    if session.get("payment_status") not in {
                        "paid",
                        "unpaid",
                        "no_payment_required",
                    }:
                        raise ValueError("invalid payment state")
                    if session["payment_status"] == "paid":
                        metadata = session["metadata"]
                        order_id = _identifier(
                            metadata["order_id"], r"[1-9][0-9]{0,18}"
                        )
                        if session.get("client_reference_id") != order_id:
                            raise ValueError("invalid reference")
                        completion = PaidCheckout(
                            _identifier(metadata["checkout_key"], r"[0-9a-f]{64}"),
                            _identifier(session.get("id"), r"cs_test_[A-Za-z0-9]+"),
                            order_id,
                            _identifier(metadata["customer_id"], r"[1-9][0-9]{0,18}"),
                            session["mode"],
                            session["currency"],
                            session["amount_total"],
                            session["created"],
                            _identifier(session.get('customer'),r'cus_[A-Za-z0-9]+') if session['mode']=='subscription' else None,
                            _identifier(session.get('subscription'),r'sub_[A-Za-z0-9]+') if session['mode']=='subscription' else None,
                        )
            except (
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                UnicodeError,
                RecursionError,
                stripe.SignatureVerificationError,
            ):
                raise ValueError("Invalid Stripe test event") from None
            if subscription_notice is not None:
                if self._subscription_service is None:
                    raise CheckoutUnavailable
                self._subscription_service.apply(subscription_notice,event_id=event_id)
                return None
            return self.store.record_event(
                event_id=event_id,
                event_type=event_type,
                completion=completion,
                now=datetime.now(timezone.utc),
            )
        finally:
            self._slots.release()
