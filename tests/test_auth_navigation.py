"""Navigation is local, token-bound and does not replace package authorization."""
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from tests.test_auth_routes import http as _http_fixture
from tests.test_auth_service import NOW
from tests.test_auth_store import (
    auth_store_fixture as _auth_store_fixture,  # noqa: F401
)

ORIGIN = "https://example.test"
TARGET = "/panel/paczki/7?page=2&category=change&case_id=CASE%2F001"


@pytest.fixture
def http(auth_store):
    yield from _http_fixture.__wrapped__(auth_store)


class Fields(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.values = {}
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.values[attrs["name"]] = attrs.get("value", "")


def issue(client, sender, target=TARGET):
    response = client.get("/logowanie", params={"next": target})
    assert response.status_code == 200
    assert Fields(response.text).values["next"] == target
    sent = client.post("/auth/magic-link", data={"email": "person@example.test", "next": target}, headers={"Origin": ORIGIN})
    assert sent.status_code == 202
    return next(line for line in sender.messages[-1][2].splitlines() if line.startswith(ORIGIN + "/auth/verify"))


def confirm(client, url, *, target=None):
    parts = urlsplit(url)
    return client.post("/auth/verify", data={"token": parts.fragment, "next": target or parse_qs(parts.query).get("next", ["/panel"])[0]}, headers={"Origin": ORIGIN}, follow_redirects=False)


def test_login_returns_to_token_bound_package_context(http):
    client, _, sender = http
    link = issue(client, sender)
    assert parse_qs(urlsplit(link).query)["next"] == [TARGET]
    for _ in range(2):
        scanner = client.get(link.split("#")[0])
        assert scanner.status_code == 200
        assert Fields(scanner.text).values["next"] == TARGET
    assert confirm(client, link).headers["location"] == TARGET
    assert confirm(client, link).status_code == 401
    assert client.get("/panel/paczki/7").status_code == 404  # destination never grants access


def test_changed_return_does_not_consume_legitimate_link(http):
    client, _, sender = http
    link = issue(client, sender)
    assert confirm(client, link, target="/panel/paczki/8").status_code == 401
    assert confirm(client, link).headers["location"] == TARGET


@pytest.mark.parametrize("target", ["https://evil.test/panel", "//evil.test/panel", "/\\evil.test/panel", "/panel/../zamowienie", "/panel/paczki/0", "/panel/paczki/9223372036854775808", "/panel/paczki/1?next=https://evil.test", "/panel/paczki/1?page=2&page=3", "/panel/paczki/1?category=bad", "/panel/paczki/1?case_id=%0aX", "/%70anel", "/panel#https://evil.test", "/panel?x=" + "x"*2048])
def test_invalid_return_rejected_before_mail(http, target):
    client, _, sender = http
    assert client.get("/logowanie", params={"next": target}).status_code == 422
    assert not sender.messages


def test_duplicate_return_is_rejected(http):
    client, _, _ = http
    assert client.get("/logowanie?next=/panel&next=/panel/paczki/1").status_code == 422


def test_expired_and_different_browser_keep_goal_for_new_link(http, monkeypatch):
    client, service, sender = http
    link = issue(client, sender)
    browser = client.cookies.get(service.browser_cookie)
    client.cookies.clear()
    rejected = confirm(client, link)
    assert rejected.status_code == 401
    assert Fields(rejected.text).values["next"] == TARGET
    assert "tej samej przeglądarce" in rejected.text
    client.cookies.set(service.browser_cookie, browser, domain="example.test", path="/")
    monkeypatch.setattr(service, "_now", lambda: NOW + timedelta(minutes=16))
    expired = confirm(client, link)
    assert expired.status_code == 401
    assert Fields(expired.text).values["next"] == TARGET
    renewed = issue(client, sender)
    assert confirm(client, renewed).headers["location"] == TARGET


def test_expired_package_redirect_preserves_context(http):
    client, _, _ = http
    response = client.get(TARGET, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/logowanie?" + urlencode({"next": TARGET})


def test_database_failure_keeps_destination_and_does_not_consume_link(http):
    from core.instance import database_replacement_lock
    client, service, sender = http
    link = issue(client, sender)
    with database_replacement_lock(service.store.path):
        failed = confirm(client, link)
    assert failed.status_code == 503
    assert Fields(failed.text).values["next"] == TARGET
    assert confirm(client, link).headers["location"] == TARGET


def test_two_links_keep_independent_destinations(http):
    client, _, sender = http
    first = issue(client, sender)
    second = issue(client, sender, "/panel/paczki/8")
    assert confirm(client, second).headers["location"] == "/panel/paczki/8"
    assert confirm(client, first).headers["location"] == TARGET


def test_return_context_does_not_leak_through_access_logs():
    import logging

    from app.security import DownloadLogRedaction
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1, "%s", ("/logowanie?next=%2Fpanel%2Fpaczki%2F7%3Fcase_id%3DPRIVATECASE",), None)
    DownloadLogRedaction().filter(record)
    assert "PRIVATECASE" not in record.getMessage()

