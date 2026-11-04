"""Authoritative test-subscription refresh outside database/OS locks."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from uuid import uuid4

import stripe

from core.auth_store import BillingRecovery, RecoveredBilling
from core.checkout_store import CheckoutConflict, CheckoutUnavailable
from core.subscription_store import (
    SubscriptionNotice,
    SubscriptionState,
    SubscriptionStore,
)

from .stripe_service import CheckoutSettings


def _id(value, pattern: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 255
        or not re.fullmatch(pattern, value)
    ):
        raise ValueError("Invalid billing identifier")
    return value


def parse_notice(value, *, deleted: bool = False) -> SubscriptionNotice | None:
    if value.get("object") != "subscription" or value.get("livemode") is not False:
        raise ValueError("Invalid test subscription")
    subscription_id = _id(value.get("id"), r"sub_[A-Za-z0-9]+")
    customer_id = _id(value.get("customer"), r"cus_[A-Za-z0-9]+")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise TypeError("Invalid metadata")
    if not {"checkout_key", "order_id", "customer_id"} <= metadata.keys():
        return None  # unrelated subscription from the same test Stripe account
    if deleted and value.get("status") not in {"canceled", "incomplete_expired"}:
        raise ValueError("Invalid terminal subscription")
    return SubscriptionNotice(
        subscription_id,
        customer_id,
        _id(metadata["checkout_key"], r"[0-9a-f]{64}"),
        _id(metadata["order_id"], r"[1-9][0-9]{0,18}"),
        _id(metadata["customer_id"], r"[1-9][0-9]{0,18}"),
        deleted,
    )


class SubscriptionService:
    def reconcile(self, notice: SubscriptionNotice) -> None:
        """Internal ownership-confirmation refresh, explicitly not a Stripe event."""
        self.apply(notice, event_id="local_reconcile_" + uuid4().hex)

    def __init__(
        self,
        store: SubscriptionStore,
        settings: CheckoutSettings,
        *,
        subscriptions=None,
        checkout_sessions=None,
        now=None,
    ):
        self.store, self.settings = store, settings
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._subscriptions = subscriptions
        self._checkout_sessions = checkout_sessions
        if subscriptions is None and settings.test_mode:
            client = stripe.StripeClient(
                settings.secret_key,
                stripe_version="2024-06-20",
                max_network_retries=0,
                http_client=stripe.RequestsClient(timeout=5),
            )
            self._subscriptions = client.subscriptions
            if checkout_sessions is None:
                self._checkout_sessions = client.checkout.sessions

    def recover_checkout(self, attempt: BillingRecovery) -> RecoveredBilling:
        """Read an existing paid test Session; never replay a payment or package."""
        if not self.settings.test_mode:
            raise CheckoutUnavailable
        try:
            session_id = _id(attempt.session_id, r"cs_test_[A-Za-z0-9]+")
            if self._checkout_sessions is None:
                self._checkout_sessions = stripe.StripeClient(
                    self.settings.secret_key,
                    stripe_version="2024-06-20",
                    max_network_retries=0,
                    http_client=stripe.RequestsClient(timeout=5),
                ).checkout.sessions
            session = self._checkout_sessions.retrieve(session_id)
            offer = json.loads(attempt.offer_json)
            metadata = session["metadata"]
            if (
                session["id"] != session_id
                or session["object"] != "checkout.session"
                or session["livemode"] is not False
                or session["mode"] != "subscription"
                or session["status"] != "complete"
                or session["payment_status"] != "paid"
                or session["currency"] != "pln"
                or type(session["amount_total"]) is not int
                or type(offer["amount_grosze"]) is not int
                or not 1 <= offer["amount_grosze"] <= 100_000_000
                or session["amount_total"] != offer["amount_grosze"]
                or offer["interval"] not in ("month", "year")
                or not isinstance(session.get("customer_email"), str)
                or session["customer_email"].lower() != attempt.email.lower()
                or session["client_reference_id"] != str(attempt.order_id)
                or metadata["order_id"] != str(attempt.order_id)
                or metadata["customer_id"] != str(attempt.customer_id)
                or metadata["checkout_key"] != attempt.key_hash
                or type(session["created"]) is not int
                or not attempt.created_at - 300
                <= session["created"]
                < attempt.expires_at
                or type(session["expires_at"]) is not int
                or session["expires_at"] != attempt.expires_at
            ):
                raise ValueError("Invalid historical Checkout proof")
            return RecoveredBilling(
                attempt,
                _id(session["customer"], r"cus_[A-Za-z0-9]+"),
                _id(session["subscription"], r"sub_[A-Za-z0-9]+"),
            )
        except (stripe.StripeError, ValueError, TypeError, KeyError, AttributeError):
            raise CheckoutUnavailable from None

    def apply(self, notice: SubscriptionNotice, *, event_id: str) -> None:
        if not self.settings.test_mode:
            raise CheckoutUnavailable
        ticket = self.store.claim_event(notice, event_id=event_id, now=self._now())
        if ticket is None:
            return
        try:
            current = self._subscriptions.retrieve(notice.subscription_id)
            if parse_notice(current) != notice:
                raise ValueError("Subscription correlation changed")
            pause = current.get("pause_collection")
            if pause is not None and (
                not isinstance(pause, dict)
                or pause.get("behavior")
                not in {"keep_as_draft", "mark_uncollectible", "void"}
            ):
                raise ValueError("Invalid collection pause")
            state = SubscriptionState(
                current["status"],
                current["current_period_end"],
                current["cancel_at_period_end"],
                pause is not None,
            )
            self.store.finish_refresh(ticket, state, now=self._now())
        except (stripe.StripeError, ValueError, TypeError, KeyError, AttributeError):
            self._release(ticket)
            raise CheckoutUnavailable from None
        except (CheckoutUnavailable, CheckoutConflict):
            self._release(ticket)
            raise

    def _release(self, ticket) -> None:
        try:
            self.store.fail_refresh(ticket)
        except CheckoutUnavailable:
            pass  # lease expires if storage is temporarily unavailable
