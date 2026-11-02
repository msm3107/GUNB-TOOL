"""Authoritative test-subscription refresh outside database/OS locks."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import stripe

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
    def __init__(
        self,
        store: SubscriptionStore,
        settings: CheckoutSettings,
        *,
        subscriptions=None,
        now=None,
    ):
        self.store, self.settings = store, settings
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._subscriptions = subscriptions
        if subscriptions is None and settings.test_mode:
            self._subscriptions = stripe.StripeClient(
                settings.secret_key,
                stripe_version="2024-06-20",
                max_network_retries=0,
                http_client=stripe.RequestsClient(timeout=5),
            ).subscriptions

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
