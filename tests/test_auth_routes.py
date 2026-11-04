import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthService
from app.config import WebConfig
from app.main import create_app
from tests.test_auth_service import NOW, Sender, settings
from tests.test_auth_store import (
    auth_store_fixture as _auth_store_fixture,  # noqa: F401
)


@pytest.fixture
def http(auth_store):
    sender = Sender(auth_store.path)
    service = AuthService(auth_store, settings(), sender=sender, now=lambda: NOW)
    config = WebConfig(database_path=auth_store.path, auth=settings())
    with TestClient(
        create_app(config, auth_service=service), base_url="https://example.test"
    ) as client:
        yield client, service, sender


def request_link(client, email="person@example.test"):
    client.get("/logowanie")
    return client.post(
        "/auth/magic-link",
        json={"email": email},
        headers={"Origin": "https://example.test"},
    )


def token(sender):
    return sender.messages[-1][2].split("/auth/verify#", 1)[1].split()[0]


def test_known_unknown_response_and_inert_get_then_atomic_cookie_login(http):
    client, service, sender = http
    known = request_link(client)
    unknown = request_link(client, "unknown@example.test")
    assert (
        known.status_code == unknown.status_code == 202
        and known.content == unknown.content
    )
    raw = token(sender)
    for _ in range(2):
        assert client.get("/auth/verify").status_code == 200
    assert client.cookies.get(service.session_cookie) is None
    response = client.post(
        "/auth/verify",
        data={"token": raw},
        headers={"Origin": "https://example.test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert (
        "__Host-zt_session=" in cookie
        and "HttpOnly" in cookie
        and "Secure" in cookie
        and "SameSite=lax" in cookie
        and "Domain=" not in cookie
    )
    assert raw not in response.text and raw not in response.headers.get("location", "")
    assert "Zalogowano" in client.get("/logowanie").text
    assert (
        client.post(
            "/auth/verify",
            data={"token": raw},
            headers={"Origin": "https://example.test"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/logout",
            headers={"Origin": "https://example.test"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert client.cookies.get(service.session_cookie) is None
    assert "Zalogowano" not in client.get("/logowanie").text


def test_different_browser_cannot_login_with_valid_attacker_link(http):
    client, service, sender = http
    request_link(client)
    raw = token(sender)
    with TestClient(
        create_app(WebConfig(auth=settings()), auth_service=service),
        base_url="https://example.test",
    ) as other:
        other.get("/logowanie")
        assert (
            other.post(
                "/auth/verify",
                data={"token": raw},
                headers={"Origin": "https://example.test"},
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/auth/verify",
            data={"token": raw},
            headers={"Origin": "https://example.test"},
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_csrf_and_extra_fields_are_rejected_without_sending(http):
    client, _, sender = http
    client.get("/logowanie")
    assert (
        client.post(
            "/auth/magic-link", json={"email": "person@example.test"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/auth/magic-link",
            json={"email": "person@example.test"},
            headers={"Origin": "https://evil.test"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/auth/magic-link",
            json={"email": "person@example.test", "customer_id": 1},
            headers={"Origin": "https://example.test"},
        ).status_code
        == 422
    )
    assert not sender.messages
    assert (
        client.post(
            "/auth/magic-link",
            content=b"x" * 4097,
            headers={
                "Origin": "https://example.test",
                "Content-Type": "application/json",
            },
        ).status_code
        == 413
    )
    assert client.post("/auth/logout").status_code == 403


def test_link_request_renews_existing_browser_cookie_for_full_link_ttl(
    http, monkeypatch
):
    import time
    from datetime import timedelta

    client, service, sender = http
    clock = [NOW]
    monkeypatch.setattr(time, "time", lambda: clock[0].timestamp())
    service._now = lambda: clock[0]
    client.get("/logowanie")
    original = client.cookies.get(service.browser_cookie)
    clock[0] = NOW + timedelta(seconds=1790)
    response = client.post(
        "/auth/magic-link",
        json={"email": "person@example.test"},
        headers={"Origin": "https://example.test"},
    )
    assert response.status_code == 202
    clock[0] = NOW + timedelta(seconds=1810)
    response = client.post(
        "/auth/verify",
        data={"token": token(sender)},
        headers={"Origin": "https://example.test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.cookies.get(service.browser_cookie) == original


def test_unsupported_media_is_415_and_duplicate_fields_are_rejected(http):
    client, _, sender = http
    client.get("/logowanie")
    assert (
        client.post(
            "/auth/magic-link",
            content="email=person@example.test",
            headers={"Origin": "https://example.test", "Content-Type": "text/plain"},
        ).status_code
        == 415
    )
    assert (
        client.post(
            "/auth/magic-link",
            content="email=person@example.test&email=other@example.test",
            headers={
                "Origin": "https://example.test",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ).status_code
        == 422
    )
    assert not sender.messages


def test_default_disabled_has_no_database_smtp_or_cookie(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled touched IO")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with TestClient(create_app(WebConfig())) as client:
        assert client.get("/logowanie").status_code == 200
        assert not client.cookies
        assert (
            client.post(
                "/auth/magic-link", json={"email": "person@example.test"}
            ).status_code
            == 503
        )


def test_login_template_and_script_have_manual_fallback_and_no_query_token(http):
    client, _, _ = http
    page = client.get("/logowanie")
    assert (
        "Żółta Tablica" in page.text
        and "15 minut" in page.text
        and "tej samej przeglądarce" in page.text
    )
    page = client.get("/auth/verify")
    assert 'name="token"' in page.text and "Potwierdź logowanie" in page.text
    assert "/static/auth-verify.js" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "same-origin"
    assert client.get("/").headers["referrer-policy"] == "no-referrer"
