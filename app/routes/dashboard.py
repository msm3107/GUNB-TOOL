"""Authenticated dashboard and same-origin native forms; no network on GET."""

from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.auth import AuthService
from app.identity import AuthenticatedCustomer, IdentityResolver
from app.routes.auth import AuthInputError, _input
from app.stripe_service import CheckoutBusy
from core.checkout_store import CheckoutConflict, CheckoutUnavailable
from core.customer_dashboard import CustomerDashboardStore
from core.issue_tracker import IssueNotAllowed, IssueRateLimitExceeded

ISSUE_TYPES = {
    "zly_telefon": "Błędny telefon",
    "to_nie_inwestor_tylko_projektant": "Kontakt do projektanta zamiast inwestora",
    "inwestycja_juz_stoi": "Inwestycja już stoi",
    "inna_uwaga": "Inna uwaga",
}
SUBSCRIPTION_LABELS = {
    "active": "Aktywny",
    "expired": "Okres upłynął",
    "paused": "Wstrzymany",
    "past_due": "Zaległa płatność",
    "unpaid": "Nieopłacony",
    "canceled": "Zakończony",
    "trialing": "Okres próbny",
    "incomplete": "Niepotwierdzony",
    "incomplete_expired": "Nieukończony",
    "unconfirmed": "Brak potwierdzenia płatności",
}


def _cursor(request, name):
    values = request.query_params.getlist(name)
    if not values:
        return None
    if (
        len(values) != 1
        or not values[0].isascii()
        or not values[0].isdigit()
        or len(values[0]) > 19
    ):
        raise ValueError
    number = int(values[0])
    if not 1 <= number < 2**63:
        raise ValueError
    return number


def _date(epoch):
    try:
        return (
            datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if epoch
            else "Niepotwierdzony"
        )
    except (ValueError, OverflowError, OSError):
        return "Niepotwierdzony"


async def _issue_input(request, *, contextual=False):
    fields = {"case_id","issue_type","description"} | ({"page","category"} if contextual else set())
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 24576:
            raise AuthInputError(413)
        raw.extend(chunk)
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/x-www-form-urlencoded"
    ):
        raise AuthInputError(415)
    try:
        pairs = parse_qsl(
            raw.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(fields),
            errors="strict",
        )
        values = dict(pairs)
        if len(pairs) != len(values) or set(values) != fields:
            raise ValueError
        return values
    except (ValueError, UnicodeError):
        raise AuthInputError(422) from None


def dashboard_router(
    store: CustomerDashboardStore,
    identity: IdentityResolver,
    auth: AuthService,
    subscriptions,
    templates,
    *,
    now=None,
    downloads_enabled=False,
):
    router = APIRouter()
    clock = now or (lambda: datetime.now(timezone.utc))

    async def render(request, principal=None, *, status=200, message="", values=None):
        snapshot = None
        order_cursor = delivery_cursor = None
        if principal is not None:
            try:
                order_cursor, delivery_cursor = (
                    _cursor(request, "orders_before"),
                    _cursor(request, "deliveries_before"),
                )
                snapshot = await run_in_threadpool(
                    store.snapshot,
                    principal.customer_id,
                    now=clock(),
                    orders_before=order_cursor,
                    deliveries_before=delivery_cursor,
                )
            except ValueError:
                status, message = (
                    422,
                    "Niepoprawny numer strony. Wróć do najnowszych pozycji.",
                )
            except CheckoutUnavailable:
                status, message = (
                    503,
                    "Panel jest chwilowo niedostępny. Spróbuj ponownie.",
                )

        def page_link(name, cursor):
            params = {
                key: value
                for key, value in (
                    ("orders_before", order_cursor),
                    ("deliveries_before", delivery_cursor),
                )
                if value is not None
            }
            params[name] = cursor
            return "/panel?" + urlencode(params)

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "page_title": "Panel klienta",
                "dashboard": snapshot,
                "authenticated": principal is not None,
                "message": message,
                "issue_values": values or {},
                "issue_types": ISSUE_TYPES,
                "subscription_labels": SUBSCRIPTION_LABELS,
                "format_period": _date,
                "page_link": page_link,
                "paged": order_cursor is not None or delivery_cursor is not None,
                "portal_enabled": subscriptions.settings.test_mode,
                "downloads_enabled": downloads_enabled,
            },
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                **({"Retry-After": "60"} if status in (429, 503) else {}),
            },
        )

    async def principal(request):
        result = await run_in_threadpool(identity, request)
        return result if isinstance(result, AuthenticatedCustomer) else None

    @router.get("/panel", include_in_schema=False, name="dashboard")
    async def dashboard(request: Request):
        try:
            owner = await principal(request)
        except CheckoutUnavailable:
            return await render(
                request,
                status=503,
                message="Panel jest chwilowo niedostępny. Spróbuj ponownie.",
            )
        if owner is None:
            return RedirectResponse(
                "/logowanie", status_code=303, headers={"Cache-Control": "no-store"}
            )
        notice = {
            "portal-error": "Portal rozliczeń jest chwilowo niedostępny lub nie potwierdzono powiązania. Spróbuj później albo potwierdź rozliczenia poniżej.",
            "issue": "Zgłoszenie zapisano lub było już wcześniej zgłoszone. Wymaga weryfikacji.",
            "billing": "Powiązano rozliczenia i uzgodniono bieżący stan.",
        }.get(request.query_params.get("notice"), "")
        return await render(request, owner, message=notice)

    @router.post("/panel/uwagi", include_in_schema=False)
    @router.post("/panel/rozliczenia", include_in_schema=False)
    async def submit(request: Request):
        try:
            owner = await principal(request)
        except CheckoutUnavailable:
            return await render(
                request,
                status=503,
                message="Panel jest chwilowo niedostępny. Spróbuj ponownie.",
            )
        if owner is None:
            return RedirectResponse(
                "/logowanie", status_code=303, headers={"Cache-Control": "no-store"}
            )
        if request.headers.get("origin") != auth.settings.origin:
            return await render(
                request,
                owner,
                status=403,
                message="Niepoprawne pochodzenie formularza. Otwórz ponownie panel.",
            )
        values = {}
        try:
            if request.url.path == "/panel/rozliczenia":
                proof = await _input(request, "request_token")
                await run_in_threadpool(auth.bind_billing, owner, proof, subscriptions)
                notice = "billing"
            else:
                values = await _issue_input(request)
                if (
                    len(values["case_id"]) > 512
                    or len(values["description"]) > 1000
                    or values["issue_type"] not in ISSUE_TYPES
                ):
                    raise ValueError
                await run_in_threadpool(
                    store.report_issue,
                    owner.customer_id,
                    values["case_id"],
                    values["issue_type"],
                    values["description"],
                    now=clock(),
                )
                notice = "issue"
        except AuthInputError as exc:
            return await render(
                request,
                owner,
                status=exc.code,
                message="Niepoprawny lub zbyt duży formularz.",
            )
        except (IssueNotAllowed, CheckoutConflict):
            return await render(
                request,
                owner,
                status=403,
                message="Nie potwierdzono uprawnień do tej sprawy lub rozliczeń.",
                values=values,
            )
        except (IssueRateLimitExceeded, CheckoutBusy):
            return await render(
                request,
                owner,
                status=429,
                message="Osiągnięto limit. Zgłoszenia: do 20 nowych uwag na godzinę; spróbuj później.",
                values=values,
            )
        except ValueError:
            return await render(
                request,
                owner,
                status=422,
                message="Sprawdź numer sprawy, typ uwagi i opis (do 1000 znaków; wymagany dla innej uwagi).",
                values={
                    k: v[: 1000 if k == "description" else 512]
                    for k, v in values.items()
                },
            )
        except CheckoutUnavailable:
            return await render(
                request,
                owner,
                status=503,
                message="Operacja jest chwilowo niedostępna. Możesz ją bezpiecznie ponowić.",
                values=values,
            )
        return RedirectResponse(
            "/panel?notice=" + notice,
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    return router
