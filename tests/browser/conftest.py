"""Owned loopback servers and synthetic DB only; no external business operations."""

import socket
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
from core.storage import LeadRepository
from tests.test_auth_service import NOW, Sender, settings


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 390, "height": 844}}


@pytest.fixture
def app_server(tmp_path):
    path = tmp_path / "browser.sqlite"
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
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(
                WebConfig(database_path=path, auth=auth_settings), auth_service=auth
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
            url=url, sender=sender, path=path, delivery_done=delivery_done
        )
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive(), "Synthetic server did not stop"
