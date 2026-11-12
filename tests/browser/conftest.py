"""Owned loopback servers and synthetic DB only; no external business operations."""

import socket
import sqlite3
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
import uvicorn

from app.auth import AuthService
from app.config import WebConfig
from app.main import create_app
from core.auth_store import AuthStore
from core.models import Investment
from core.places import import_catalog
from core.storage import LeadRepository
from tests.test_auth_service import NOW, Sender, settings
from tests.test_places import DAY, gml


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 390, "height": 844}}


@pytest.fixture
def app_server(tmp_path, request):
    path = tmp_path / "browser.sqlite"
    place_source, places_path = tmp_path / "names.gml", tmp_path / "places.sqlite"
    place_source.write_text(
        gml(
            [
                ("2", "Łódź", "Gmina A", "Powiat A", "2814011", "53 20"),
                ("3", "Łódź", "Gmina B", "Powiat B", "2801011", "53.5 20.5"),
            ]
        ),
        encoding="utf-8",
    )
    place_version = import_catalog(place_source, places_path, source_date=DAY)
    with LeadRepository(path) as repo:
        repo.connection.execute(
            "INSERT INTO b2b_customers(email) VALUES('person@example.test')"
        )
        for code in ("2814", "2801", "1401"):
            repo.upsert(
                Investment(
                    id_sprawy="SYNTHETIC/" + code,
                    zrodlo="pozwolenia",
                    status="decyzja",
                    powiat_teryt=code,
                    powiat="Powiat testowy " + code,
                    kategoria="komercyjna",
                    nazwa_zamierzenia="Syntetyczna budowa",
                )
            )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = "http://127.0.0.1:" + str(sock.getsockname()[1])
    auth_settings = replace(settings(), origin=url)
    sender = Sender(path)
    delivery_done = threading.Event()

    class CompletedAuthService(AuthService):
        def deliver(self, mail):
            try:
                super().deliver(mail)
            finally:
                delivery_done.set()

    auth = CompletedAuthService(
        AuthStore(path), auth_settings, sender=sender, now=lambda: NOW
    )
    checkout_api = checkout_service = subscription_service = None
    from app.stripe_service import CheckoutService, CheckoutSettings
    checkout_config = CheckoutSettings()
    if getattr(request, "param", False):
        import copy

        import stripe

        from app.subscription_service import SubscriptionService
        from core.checkout_store import CheckoutStore
        from core.subscription_store import SubscriptionNotice, SubscriptionStore
        from tests.test_stripe_service import Customers
        from tests.test_stripe_service import settings as checkout_settings
        from tests.test_subscription_service import Subscriptions, snapshot
        checkout_config = replace(checkout_settings(), origin=url)
        class NativeSessions:
            def __init__(self):
                self.calls = []
            def create(self, params, options):
                # Another writer can commit while the provider is called.
                with sqlite3.connect(path, timeout=0) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                self.calls.append((copy.deepcopy(params), dict(options)))
                if len(self.calls) == 1:
                    raise stripe.APIConnectionError("synthetic timeout")
                return {"id": "cs_test_native", "livemode": False, "url": "https://checkout.stripe.com/test-owned"}
        checkout_api = NativeSessions()
        class NativeCustomers(Customers):
            def create(self, params, options):
                value = super().create(params, options)
                value["id"] = "cus_native"
                return value
        checkout_service = CheckoutService(CheckoutStore(path), checkout_config, sessions=checkout_api, customers=NativeCustomers(path), now=lambda: NOW)
        class NativeSubscriptions:
            def retrieve(self, subscription_id):
                metadata = checkout_api.calls[-1][0]["metadata"]
                notice = SubscriptionNotice("sub_native", "cus_native", metadata["checkout_key"], metadata["order_id"], metadata["customer_id"])
                api = Subscriptions(SubscriptionStore(path), notice)
                api.value = snapshot(notice, current_period_end=int(time.time()) + 86400)
                return api.retrieve(subscription_id)
        subscription_service = SubscriptionService(SubscriptionStore(path), checkout_config, subscriptions=NativeSubscriptions(), now=lambda: NOW)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(
                WebConfig(
                    database_path=path, places_path=places_path, auth=auth_settings,
                    package_dir=tmp_path / "private", checkout=checkout_config,
                ),
                auth_service=auth,
                checkout_service=checkout_service, subscription_service=subscription_service,
            ),
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Synthetic server did not start")
            time.sleep(0.01)
        yield SimpleNamespace(
            url=url,
            sender=sender,
            path=path,
            delivery_done=delivery_done,
            places_path=places_path,
            place_source=place_source,
            place_version=place_version,
            checkout_api=checkout_api,
        )
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive(), "Synthetic server did not stop"
