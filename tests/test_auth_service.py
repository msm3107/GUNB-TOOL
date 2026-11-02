import sqlite3
from datetime import timedelta

import pytest
from starlette.requests import Request

from app.auth import AuthService, AuthSettings, TokenCodec
from core.auth_store import AuthRejected, AuthStore
from core.email_sender import DeliveryResult, SMTPSettings
from core.instance import database_replacement_lock
from tests.test_auth_store import (
    auth_store_fixture as _auth_store_fixture,  # noqa: F401
)
from tests.test_payment_store import NOW

BROWSER = "x" * 43


def settings(**changes):
    values = {
        "enabled": True,
        "secret_key": "a" * 64,
        "origin": "https://example.test",
        "smtp": SMTPSettings("smtp.example.test", 587, "login@example.test"),
    }
    values.update(changes)
    return AuthSettings(**values)


class Sender:
    def __init__(self, path):
        self.path = path
        self.messages = []
        self.outcome = "accepted"

    def send_email(self, recipient, subject, body, *, idempotency_key):
        with (
            database_replacement_lock(self.path),
            sqlite3.connect(self.path, timeout=0.1) as c,
        ):
            c.execute("BEGIN IMMEDIATE")
        self.messages.append((recipient, subject, body, idempotency_key))
        return DeliveryResult(self.outcome)


@pytest.fixture
def service(auth_store):
    sender = Sender(auth_store.path)
    return AuthService(auth_store, settings(), sender=sender, now=lambda: NOW), sender


def request(service, token):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", f"{service.session_cookie}={token}".encode())],
        }
    )


def test_hmac_ttl_future_tampering_and_purpose_separation():
    codec = TokenCodec("a" * 64)
    link = codec.issue("magic", NOW)
    assert codec.valid(link, "magic", NOW)
    assert not codec.valid(link, "session", NOW)
    assert not codec.valid(link, "magic", NOW + timedelta(minutes=15))
    assert not codec.valid(link, "magic", NOW - timedelta(seconds=31))
    assert not codec.valid(link[:-1] + ("0" if link[-1] != "0" else "1"), "magic", NOW)
    assert not TokenCodec("b" * 64).valid(link, "magic", NOW)


def test_mail_contains_fragment_and_no_raw_token_is_persisted(service):
    instance, sender = service
    mail = instance.prepare("person@example.test", BROWSER, "127.0.0.1")
    assert mail is not None
    assert mail.token not in repr(mail)
    instance.deliver(mail)
    recipient, subject, body, key = sender.messages[0]
    assert recipient == "person@example.test" and "Żółta Tablica" in subject
    assert "/auth/verify#" + mail.token in body and "?token=" not in body
    assert mail.token not in key
    with sqlite3.connect(instance.store.path) as c:
        assert mail.token not in str(
            c.execute("SELECT * FROM auth_magic_links").fetchall()
        )
        assert c.execute("SELECT delivery_status FROM auth_magic_links").fetchone() == (
            "accepted",
        )
    session = instance.verify(mail.token, BROWSER)
    assert session not in repr(instance.settings)
    assert instance.resolve(request(instance, session)).customer_id == 1
    with pytest.raises(AuthRejected):
        instance.verify(mail.token, BROWSER)


def test_unknown_rate_limit_and_wrong_browser_do_not_authenticate(service):
    instance, sender = service
    assert instance.prepare("unknown@example.test", BROWSER, "127.0.0.1") is None
    assert not sender.messages
    mail = instance.prepare("person@example.test", BROWSER, "127.0.0.1")
    with pytest.raises(AuthRejected):
        instance.verify(mail.token, "y" * 43)
    assert instance.resolve(request(instance, mail.token)) is None
    instance.verify(mail.token, BROWSER)


def test_smtp_unknown_does_not_burn_possibly_delivered_link(service):
    instance, sender = service
    sender.outcome = "unknown"
    mail = instance.prepare("person@example.test", BROWSER, "127.0.0.1")
    instance.deliver(mail)
    assert instance.verify(mail.token, BROWSER)


def test_disabled_auth_touches_no_db_or_smtp(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled IO")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    instance = AuthService(AuthStore(None), AuthSettings())
    assert instance.resolve(request(instance, "forged")) is None


def test_overloaded_delivery_is_bounded_and_marked_retry(service):
    instance, sender = service
    mail = instance.prepare("person@example.test", BROWSER, "127.0.0.1")
    assert instance._send_slots.acquire(blocking=False)
    assert instance._send_slots.acquire(blocking=False)
    try:
        instance.deliver(mail)
    finally:
        instance._send_slots.release()
        instance._send_slots.release()
    assert not sender.messages
    with sqlite3.connect(instance.store.path) as c:
        assert c.execute("SELECT delivery_status FROM auth_magic_links").fetchone() == (
            "retry",
        )


def test_env_configuration_is_explicit_test_only_and_secrets_are_hidden():
    assert not AuthSettings.from_env({}).enabled
    source = {
        "GUNB_WEB_AUTH_MODE": "test",
        "GUNB_WEB_AUTH_SECRET": "a" * 64,
        "GUNB_WEB_AUTH_ORIGIN": "https://example.test",
        "GUNB_WEB_AUTH_SMTP_HOST": "smtp.example.test",
        "GUNB_WEB_AUTH_SMTP_FROM": "login@example.test",
        "GUNB_WEB_AUTH_SMTP_USERNAME": "synthetic",
        "GUNB_WEB_AUTH_SMTP_PASSWORD": "synthetic-password",
    }
    config = AuthSettings.from_env(source)
    assert (
        config.enabled
        and "a" * 64 not in repr(config)
        and "synthetic-password" not in repr(config)
    )
    with pytest.raises(ValueError):
        AuthSettings.from_env(dict(source, GUNB_WEB_AUTH_MODE="live"))


@pytest.mark.parametrize(
    "changes",
    [
        {"secret_key": "short"},
        {"origin": "http://public.example.test"},
        {"origin": "https://user@example.test"},
        {"smtp": None},
    ],
)
def test_auth_config_rejects_unsafe_enabled_settings(changes):
    with pytest.raises(ValueError):
        settings(**changes)
