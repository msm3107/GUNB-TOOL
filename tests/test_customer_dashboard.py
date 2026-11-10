import sqlite3
from contextlib import ExitStack, contextmanager
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthService
from app.config import WebConfig
from app.main import create_app
from core.auth_store import AuthStore
from core.storage import LeadRepository
from tests.test_auth_billing import UUID, login
from tests.test_auth_billing import (
    billing_http as _billing_http,  # noqa: F401
)
from tests.test_auth_service import NOW, Sender, settings
from tests.test_auth_store import (
    auth_store_fixture as _auth_store_fixture,  # noqa: F401
)
from tests.test_issue_tracker import deliver_case


class _IssueForm(HTMLParser):
    """Read the retry payload from rendered HTML, including decoded entities."""

    def __init__(self, html, action="/panel/uwagi"):
        super().__init__()
        self.values = {}
        self.action = action
        self.active = False
        self.textarea = self.select = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.active = attrs.get("action") == self.action
        if not self.active:
            return
        if tag == "input":
            self.values[attrs.get("name")] = attrs.get("value", "")
        elif tag == "textarea":
            self.textarea = attrs["name"]
            self.values[self.textarea] = ""
        elif tag == "select":
            self.select = attrs["name"]
        elif tag == "option" and "selected" in attrs:
            self.values[self.select] = attrs["value"]

    def handle_endtag(self, tag):
        if tag == "form":
            self.active = False
        elif tag == "textarea":
            self.textarea = None
        elif tag == "select":
            self.select = None

    def handle_data(self, data):
        if self.active and self.textarea:
            self.values[self.textarea] += data


@contextmanager
def _lock_after_real_auth(monkeypatch, path):
    from core.instance import database_replacement_lock

    original = AuthStore.session_customer
    with ExitStack() as locks, monkeypatch.context() as patch:

        def session_then_lock(self, *args, **kwargs):
            customer_id = original(self, *args, **kwargs)
            assert customer_id == 1  # the real session was checked before the fault
            locks.enter_context(database_replacement_lock(path))
            return customer_id

        patch.setattr(AuthStore, "session_customer", session_then_lock)
        yield


@pytest.fixture
def dashboard(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    with LeadRepository(path) as repo:
        deliver_case(repo, "OWN/CASE")
        deliver_case(repo, "OTHER/SECRET", customer_id=2)
        deliver_case(repo, "PREPARED", delivered=False)
        repo.connection.execute(
            "UPDATE b2b_customers SET email='person@example.test',nazwa_firmy='<script>owner</script>' WHERE customer_id=1"
        )
        repo.connection.execute(
            "UPDATE b2b_customers SET nazwa_firmy='FOREIGN_COMPANY' WHERE customer_id=2"
        )
        repo.connection.execute(
            "UPDATE b2b_deliveries SET plik_xlsx='/private/SECRET.xlsx'"
        )
    sender = Sender(path)
    auth = AuthService(AuthStore(path), settings(), sender=sender, now=lambda: NOW)
    config = WebConfig(database_path=path, auth=settings())
    with TestClient(
        create_app(config, auth_service=auth), base_url="https://example.test"
    ) as client:
        yield client, sender, path


def test_anonymous_dashboard_redirects_and_real_login_sees_only_own_data(dashboard):
    client, sender, _ = dashboard
    response = client.get("/panel?customer_id=2", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/logowanie"
    login(client, sender)
    response = client.get("/panel?customer_id=2")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "person@example.test" in response.text
    assert (
        "FOREIGN_COMPANY" not in response.text and "OTHER/SECRET" not in response.text
    )
    assert "/private/SECRET.xlsx" not in response.text
    assert "&lt;script&gt;owner&lt;/script&gt;" in response.text
    assert "Przygotowana" in response.text and "Doręczona" in response.text


def test_panel_has_local_section_navigation_and_secondary_logout(dashboard):
    client, sender, _ = dashboard
    login(client, sender)
    response = client.get("/panel")
    header = response.text.split("</header>", 1)[0]
    for anchor in ("packages-heading", "orders-heading", "subscription-heading"):
        assert f'href="/panel#{anchor}"' in header
    assert 'class="button-secondary" type="submit">Wyloguj się' in response.text
    assert 'class="work-page"' in response.text


def test_dashboard_issue_prg_retry_and_ownership(dashboard):
    client, sender, path = dashboard
    login(client, sender)
    payload = {
        "case_id": "OWN/CASE",
        "issue_type": "inna_uwaga",
        "description": "<script>plain</script>",
    }
    for _ in range(2):
        response = client.post(
            "/panel/uwagi",
            data=payload,
            headers={"Origin": "https://example.test"},
            follow_redirects=False,
        )
        assert (
            response.status_code == 303
            and response.headers["location"] == "/panel?notice=issue"
        )
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*),min(opis) FROM issue_reports").fetchone() == (
            1,
            "<script>plain</script>",
        )
    for case in ("OTHER/SECRET", "PREPARED", "MISSING"):
        response = client.post(
            "/panel/uwagi",
            data={**payload, "case_id": case},
            headers={"Origin": "https://example.test"},
        )
        assert response.status_code == 403
        assert "<script>plain</script>" not in response.text


@pytest.mark.parametrize("origin", (None, "null", "https://other.test"))
def test_dashboard_writes_reject_bad_origin(dashboard, origin):
    client, sender, path = dashboard
    login(client, sender)
    response = client.post(
        "/panel/uwagi",
        data={"case_id": "OWN/CASE", "issue_type": "zly_telefon", "description": ""},
        headers={} if origin is None else {"Origin": origin},
    )
    assert response.status_code == 403
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*) FROM issue_reports").fetchone() == (0,)


@pytest.mark.parametrize(
    "data,status",
    [
        (b"case_id=OWN%2FCASE&issue_type=inna_uwaga&description=", 422),
        (b"case_id=OWN%2FCASE&case_id=OTHER&issue_type=zly_telefon&description=", 422),
        (b"case_id=OWN%2FCASE&issue_type=zly_telefon&description=&customer_id=2", 422),
        (b"x" * 32768, 413),
        (b"case_id=%FF&issue_type=zly_telefon&description=", 422),
    ],
    ids=["empty-other", "duplicate-key", "forged-owner", "oversize", "invalid-utf8"],
)
def test_bad_forms_preserve_safe_fields_without_write(dashboard, data, status):
    client, sender, path = dashboard
    login(client, sender)
    response = client.post(
        "/panel/uwagi",
        content=data,
        headers={
            "Origin": "https://example.test",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert (
        response.status_code == status
        and response.headers["cache-control"] == "no-store"
    )
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*) FROM issue_reports").fetchone() == (0,)


def test_core_dashboard_counts_cursor_and_wal_reader(dashboard):
    from core.customer_dashboard import CustomerDashboardStore

    _, _, path = dashboard
    store = CustomerDashboardStore(path)
    result = store.snapshot(1, now=NOW)
    assert [d.delivery_id for d in result.deliveries] == [3, 1]
    assert result.deliveries[1].full_count == 1
    assert result.deliveries[1].new_count == result.deliveries[1].change_count == 0
    assert all(o.order_id != 2 for o in result.orders)
    with sqlite3.connect(path) as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "UPDATE b2b_customers SET nazwa_firmy='uncommitted' WHERE customer_id=1"
        )
        assert store.snapshot(1, now=NOW).company == "<script>owner</script>"


@pytest.mark.parametrize(
    "query",
    (
        "orders_before=-1",
        "deliveries_before=9223372036854775808",
        "orders_before=x",
        "orders_before=1&orders_before=2",
    ),
)
def test_invalid_pagination_is_rejected(dashboard, query):
    client, sender, _ = dashboard
    login(client, sender)
    assert client.get("/panel?" + query).status_code == 422


def test_dashboard_does_not_create_database(tmp_path):
    from core.checkout_store import CheckoutUnavailable
    from core.customer_dashboard import CustomerDashboardStore

    path = tmp_path / "missing.sqlite"
    with pytest.raises(CheckoutUnavailable):
        CustomerDashboardStore(path).snapshot(1, now=NOW)
    assert not path.exists()


def test_pagination_is_tenant_scoped_and_counts_are_per_package(dashboard):
    from core.customer_dashboard import CustomerDashboardStore

    _, _, path = dashboard
    with LeadRepository(path) as repo:
        for i in range(45):
            deliver_case(repo, f"PAGE/{i}")
        repo.connection.execute(
            "UPDATE b2b_delivery_items SET kategoria_dostawy='change' WHERE delivery_id=47"
        )
        repo.connection.execute(
            "UPDATE b2b_delivery_items SET kategoria_dostawy='new' WHERE delivery_id=48"
        )
    store = CustomerDashboardStore(path)
    pages = []
    cursor = None
    for _ in range(3):
        result = store.snapshot(
            1, now=NOW, deliveries_before=cursor, orders_before=cursor
        )
        pages.extend(d.delivery_id for d in result.deliveries)
        cursor = result.deliveries_next
    assert pages == list(range(48, 2, -1)) + [1]
    assert cursor is None
    first = store.snapshot(1, now=NOW)
    assert len(first.orders) == len(first.deliveries) == 20
    assert first.deliveries[0].new_count == first.deliveries[1].change_count == 1
    assert first.deliveries[0].full_count == 0


def test_unicode_description_boundary_and_validation_preserves_values(dashboard):
    client, sender, path = dashboard
    login(client, sender)
    values = {"case_id": "OWN/CASE", "issue_type": "inna_uwaga", "description": ""}
    response = client.post(
        "/panel/uwagi", data=values, headers={"Origin": "https://example.test"}
    )
    assert response.status_code == 422 and 'value="OWN/CASE"' in response.text
    values["description"] = "🧱" * 1000
    assert (
        client.post(
            "/panel/uwagi",
            data=values,
            headers={"Origin": "https://example.test"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT length(opis) FROM issue_reports").fetchone() == (1000,)


def test_issue_hourly_limit_and_database_failure_are_retryable(dashboard):
    client, sender, path = dashboard
    login(client, sender)
    with LeadRepository(path) as repo:
        for i in range(21):
            deliver_case(repo, f"LIMIT/{i}")
    for i in range(20):
        assert (
            client.post(
                "/panel/uwagi",
                data={
                    "case_id": f"LIMIT/{i}",
                    "issue_type": "zly_telefon",
                    "description": "",
                },
                headers={"Origin": "https://example.test"},
                follow_redirects=False,
            ).status_code
            == 303
        )
    response = client.post(
        "/panel/uwagi",
        data={"case_id": "LIMIT/20", "issue_type": "zly_telefon", "description": ""},
        headers={"Origin": "https://example.test"},
    )
    assert response.status_code == 429 and response.headers["retry-after"] == "60"
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA user_version=21")
    response = client.get("/panel")
    assert response.status_code == 503 and "person@example.test" not in response.text
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone() == (21,)


def test_native_billing_proof_retry_and_portal_use_real_session(_billing_http):  # noqa: F811
    client, _, sender, _, api, portal = _billing_http
    login(client, sender)
    response = client.post(
        "/panel/rozliczenia",
        data={"request_token": UUID},
        headers={"Origin": "https://example.test"},
        follow_redirects=False,
    )
    assert (
        response.status_code == 303
        and response.headers["location"] == "/panel?notice=billing"
    )
    for _ in range(2):
        page = client.get("/panel")
        assert page.status_code == 200 and 'action="/panel/portal"' in page.text
        assert (
            "form-action 'self' https://billing.stripe.com;"
            in page.headers["content-security-policy"]
        )
        assert (
            UUID not in page.text
            and "cus_synthetic" not in page.text
            and "sub_synthetic" not in page.text
        )
    assert api.calls == 1  # GET is local, never calls Stripe
    assert (
        "billing.stripe.com" not in client.get("/").headers["content-security-policy"]
    )
    response = client.post(
        "/panel/portal",
        headers={"Origin": "https://example.test"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and len(portal.calls) == 1
    assert portal.calls[0]["return_url"] == "https://example.test/panel"
    api.failure = True
    response = client.post(
        "/panel/rozliczenia",
        data={"request_token": UUID},
        headers={"Origin": "https://example.test"},
    )
    assert response.status_code == 503 and UUID not in response.text


@pytest.mark.parametrize(
    "change,expected",
    [
        ("", "active"),
        ("UPDATE b2b_subscriptions SET current_period_end=0", "expired"),
        ("UPDATE b2b_subscriptions SET terminal=1", "canceled"),
        ("UPDATE b2b_subscriptions SET paused=1", "paused"),
        ("UPDATE b2b_orders SET status_platnosci='pending'", "unconfirmed"),
        ("UPDATE b2b_customers SET stripe_customer_id=NULL", None),
    ],
)
def test_dashboard_subscription_never_uses_stale_customer_projection(
    _billing_http,  # noqa: F811
    change,
    expected,
):
    from core.customer_dashboard import CustomerDashboardStore

    client, _, sender, store, _, _ = _billing_http
    login(client, sender)
    client.post(
        "/panel/rozliczenia",
        data={"request_token": UUID},
        headers={"Origin": "https://example.test"},
    )
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE b2b_customers SET status_subskrypcji='active'")
        if change:
            c.execute(change)
    result = CustomerDashboardStore(store.path).snapshot(1, now=NOW)
    assert (
        result.subscriptions[0].status if result.subscriptions else None
    ) == expected


def test_empty_account_and_malformed_historical_filters(dashboard):
    from core.customer_dashboard import CustomerDashboardStore

    _, _, path = dashboard
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT INTO b2b_customers(customer_id,email) VALUES(3,'empty@example.test')"
        )
        c.execute("UPDATE b2b_orders SET filtry_json='[1,2,3]' WHERE customer_id=1")
    store = CustomerDashboardStore(path)
    empty = store.snapshot(3, now=NOW)
    assert empty.orders == empty.deliveries == empty.subscriptions == ()
    assert not empty.billing_linked
    assert store.snapshot(1, now=NOW).orders[0].scope == "Zakres zapisany w zamówieniu"


def test_maintenance_lock_and_query_deadline_fail_without_writes(dashboard):
    from core.checkout_store import CheckoutUnavailable
    from core.customer_dashboard import CustomerDashboardStore
    from core.instance import database_replacement_lock

    client, sender, path = dashboard
    login(client, sender)
    with database_replacement_lock(path):
        response = client.get("/panel")
        assert response.status_code == 503
        assert "person@example.test" not in response.text
    with sqlite3.connect(path) as c:
        c.executemany(
            "INSERT INTO b2b_orders(customer_id,typ_pakietu) VALUES(1,'jednorazowy')",
            [()] * 1000,
        )
    with pytest.raises(CheckoutUnavailable):
        CustomerDashboardStore(path, query_seconds=0).snapshot(1, now=NOW)
    assert len(CustomerDashboardStore(path).snapshot(1, now=NOW).orders) == 20


def test_unauthenticated_post_cannot_use_forged_identity(dashboard):
    client, _, path = dashboard
    response = client.post(
        "/panel/uwagi?customer_id=1",
        data={"case_id": "OWN/CASE", "issue_type": "zly_telefon", "description": ""},
        headers={"Origin": "https://example.test", "X-Customer-ID": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/logowanie"
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*) FROM issue_reports").fetchone() == (0,)


@pytest.mark.parametrize(
    "failure,status", [("sdk", 503), ("rate", 429), ("mapping", 403)]
)
def test_native_portal_errors_return_to_panel_but_api_keeps_json(
    _billing_http,  # noqa: F811
    failure,
    status,
    monkeypatch,
):
    client, _, sender, store, _, portal = _billing_http
    login(client, sender)
    assert (
        client.post(
            "/panel/rozliczenia",
            data={"request_token": UUID},
            headers={"Origin": "https://example.test"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    if failure == "sdk":
        portal.fail = True
    elif failure == "rate":
        from app.portal_service import PortalService
        from app.stripe_service import CheckoutBusy

        def limited(self, principal):
            raise CheckoutBusy

        monkeypatch.setattr(PortalService, "start", limited)
    else:
        with sqlite3.connect(store.path) as c:
            c.execute("UPDATE b2b_customers SET stripe_customer_id=NULL")
    native = client.post(
        "/panel/portal",
        headers={"Origin": "https://example.test", "Accept": "text/html"},
        follow_redirects=False,
    )
    assert (
        native.status_code == 303
        and native.headers["location"] == "/panel?notice=portal-error"
    )
    assert native.headers["cache-control"] == "no-store"
    page = client.get(native.headers["location"])
    assert (
        page.status_code == 200
        and "Portal rozliczeń jest chwilowo niedostępny" in page.text
    )
    assert "cus_synthetic" not in page.text and "billing.stripe.com/p/" not in page.text
    api = client.post(
        "/panel/portal",
        headers={"Origin": "https://example.test", "Accept": "application/json"},
        follow_redirects=False,
    )
    assert api.status_code == status and api.headers["content-type"].startswith(
        "application/json"
    )


def test_issue_lock_after_auth_preserves_escaped_form_then_retry_saves_once(
    dashboard, monkeypatch
):
    client, sender, path = dashboard
    login(client, sender)
    values = {
        "case_id": "OWN/CASE",
        "issue_type": "inna_uwaga",
        "description": 'Niezapisany opis </textarea><script>alert("synthetic")</script> & "cytat"',
    }
    with _lock_after_real_auth(monkeypatch, path):
        response = client.post(
            "/panel/uwagi",
            data=values,
            headers={"Origin": "https://example.test"},
            follow_redirects=False,
        )
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["retry-after"] == "60"
        retry_values = _IssueForm(response.text).values
        assert retry_values == values
        assert "&lt;/textarea&gt;&lt;script&gt;" in response.text
        assert "<script>" not in response.text
        assert "person@example.test" not in response.text
        assert "FOREIGN_COMPANY" not in response.text
        with sqlite3.connect(path) as c:
            assert c.execute("SELECT count(*) FROM issue_reports").fetchone() == (0,)
            assert c.execute(
                "SELECT sum(wymaga_weryfikacji) FROM investments"
            ).fetchone() == (0,)
    # Retry the actual fields recovered from the503 HTML, then repeat that POST.
    for _ in range(2):
        response = client.post(
            "/panel/uwagi",
            data=retry_values,
            headers={"Origin": "https://example.test"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/panel?notice=issue"
    with sqlite3.connect(path) as c:
        assert c.execute(
            "SELECT customer_id,id_sprawy,typ,opis FROM issue_reports"
        ).fetchall() == [(1, "OWN/CASE", "inna_uwaga", values["description"])]


def test_issue_retry_form_is_not_exposed_when_session_cannot_be_verified(dashboard):
    from core.instance import database_replacement_lock

    client, sender, path = dashboard
    login(client, sender)
    with database_replacement_lock(path):
        response = client.post(
            "/panel/uwagi",
            data={
                "case_id": "OWN/CASE",
                "issue_type": "inna_uwaga",
                "description": "UNPARSED_PRIVATE_INPUT",
            },
            headers={"Origin": "https://example.test"},
        )
    assert response.status_code == 503
    assert 'action="/panel/uwagi"' not in response.text
    assert "UNPARSED_PRIVATE_INPUT" not in response.text


@pytest.mark.parametrize("page", ["get", "unsafe-case", "billing"])
def test_authenticated_snapshot_failure_keeps_only_safe_issue_form(
    dashboard, monkeypatch, page
):
    client, sender, path = dashboard
    login(client, sender)
    with _lock_after_real_auth(monkeypatch, path):
        if page == "get":
            response = client.get("/panel")
        else:
            values = {
                "case_id": '"><img src=x onerror="alert(1)">',
                "issue_type": "inna_uwaga",
                "description": "Opis pozostaje przy formularzu",
            }
            response = client.post(
                "/panel/rozliczenia" if page == "billing" else "/panel/uwagi",
                data={"request_token": UUID} if page == "billing" else values,
                headers={"Origin": "https://example.test"},
            )
    assert response.status_code == 503
    form = _IssueForm(response.text)
    assert 'action="/panel/uwagi"' in response.text
    if page == "unsafe-case":
        assert form.values == values
        assert "<img src=x" not in response.text
    else:
        assert form.values["case_id"] == form.values["description"] == ""
    assert UUID not in response.text
    assert "person@example.test" not in response.text
    assert "<script>owner</script>" not in response.text
    assert "FOREIGN_COMPANY" not in response.text
    assert "/private/SECRET.xlsx" not in response.text
    assert 'action="/panel/portal"' not in response.text
