"""Same-origin, bounded authentication forms; GET never consumes a mail token."""

import json
import secrets
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.auth import AuthService, valid_browser
from app.stripe_service import CheckoutBusy
from app.subscription_service import SubscriptionService
from core.auth_store import AuthRejected
from core.checkout_store import CheckoutConflict, CheckoutUnavailable

GENERIC = "Jeśli możemy zalogować ten adres, wyślemy link. Sprawdź także spam."


class AuthInputError(ValueError):
    def __init__(self, code):
        self.code = code


async def _input(request: Request, field: str) -> str:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 4096:
            raise AuthInputError(413)
        raw.extend(chunk)
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:

        def unique(pairs):
            result = {}
            for k, v in pairs:
                if k in result:
                    raise ValueError
                result[k] = v
            return result

        if media == "application/json":
            values = json.loads(raw, object_pairs_hook=unique)
        elif media == "application/x-www-form-urlencoded":
            values = unique(
                parse_qsl(
                    raw.decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=3,
                )
            )
        else:
            raise AuthInputError(415)
        if (
            not isinstance(values, dict)
            or set(values) != {field}
            or not isinstance(values[field], str)
        ):
            raise ValueError
        return values[field]
    except AuthInputError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AuthInputError(422) from None


def auth_router(
    service: AuthService, subscriptions: SubscriptionService, templates
) -> APIRouter:
    router = APIRouter()

    def page(request, status=200, view="login", message="", logged_in=False):
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={
                "page_title": "Logowanie bez hasła",
                "auth_enabled": service.settings.enabled,
                "view": view,
                "message": message,
                "logged_in": logged_in,
                "signed_out": request.method == 'GET' and status == 200 and service.settings.enabled and not logged_in,
            },
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                **({"Retry-After": "60"} if status in (429, 503) else {}),
            },
        )

    def error(
        request,
        code,
        message="Niepoprawne żądanie lub logowanie jest chwilowo niedostępne.",
    ):
        if request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse(
                {"detail": message},
                status_code=code,
                headers={
                    "Cache-Control": "no-store",
                    **({"Retry-After": "60"} if code in (429, 503) else {}),
                },
            )
        return page(request, code, message=message)

    def allowed(request):
        return request.headers.get("origin") == service.settings.origin

    @router.get("/logowanie", include_in_schema=False, name="login")
    async def login(request: Request):
        try:
            principal = await run_in_threadpool(service.resolve, request)
        except CheckoutUnavailable:
            return error(request, 503)
        response = page(request, logged_in=principal is not None)
        if service.settings.enabled and not valid_browser(
            request.cookies.get(service.browser_cookie)
        ):
            response.set_cookie(
                service.browser_cookie,
                secrets.token_urlsafe(32),
                max_age=1800,
                httponly=True,
                secure=service.secure,
                samesite="lax",
                path="/",
            )
        return response

    @router.post("/auth/magic-link", include_in_schema=False)
    async def magic_link(request: Request):
        if not service.settings.enabled:
            return error(request, 503)
        if not allowed(request):
            return error(request, 403)
        try:
            email = await _input(request, "email")
            if not 3 <= len(email) <= 254:
                raise ValueError
            browser = request.cookies.get(service.browser_cookie)
            mail = await run_in_threadpool(
                service.prepare,
                email,
                browser,
                request.client.host if request.client else "unknown",
            )
        except AuthInputError as exc:
            return error(request, exc.code)
        except AuthRejected:
            return error(request, 403)
        except ValueError:
            return error(request, 422, "Podaj poprawny adres e-mail.")
        except CheckoutUnavailable:
            return error(request, 503)
        response = (
            JSONResponse(
                {"detail": GENERIC},
                status_code=202,
                headers={"Cache-Control": "no-store"},
            )
            if request.headers.get("content-type", "").startswith("application/json")
            else page(request, 202, view="requested", message=GENERIC)
        )
        # A freshly issued 15-minute link must not outlive an older browser
        # cookie. Preserve its nonce so other outstanding links still work.
        response.set_cookie(
            service.browser_cookie,
            browser,
            max_age=1800,
            httponly=True,
            secure=service.secure,
            samesite="lax",
            path="/",
        )
        if mail is not None:
            response.background = BackgroundTask(service.deliver, mail)
        return response

    @router.get("/auth/verify", include_in_schema=False)
    async def confirm(request: Request):
        return page(request, view="verify")

    @router.post("/auth/verify", include_in_schema=False)
    async def verify(request: Request):
        if not service.settings.enabled:
            return error(request, 503)
        if not allowed(request):
            return error(request, 403)
        try:
            token = await _input(request, "token")
            session = await run_in_threadpool(
                service.verify,
                token,
                request.cookies.get(service.browser_cookie),
                request.cookies.get(service.session_cookie),
            )
        except AuthInputError as exc:
            return error(request, exc.code)
        except AuthRejected:
            return error(
                request,
                401,
                "Link jest nieprawidłowy, wygasł lub pochodzi z innej przeglądarki. Zamów nowy link.",
            )
        except CheckoutUnavailable:
            return error(request, 503)
        response = RedirectResponse(
            "/panel", status_code=303, headers={"Cache-Control": "no-store"}
        )
        response.set_cookie(
            service.session_cookie,
            session,
            max_age=28800,
            httponly=True,
            secure=service.secure,
            samesite="lax",
            path="/",
        )
        return response

    @router.post("/auth/logout", include_in_schema=False)
    async def logout(request: Request):
        if not service.settings.enabled:
            return error(request, 503)
        if not allowed(request):
            return error(request, 403)
        async for chunk in request.stream():
            if chunk:
                return error(request, 400)
        try:
            await run_in_threadpool(service.logout, request)
        except CheckoutUnavailable:
            return error(request, 503)
        response = RedirectResponse(
            "/logowanie", status_code=303, headers={"Cache-Control": "no-store"}
        )
        for cookie in (service.session_cookie, service.browser_cookie):
            response.delete_cookie(
                cookie, path="/", secure=service.secure, httponly=True, samesite="lax"
            )
        return response

    @router.post("/auth/billing-link", include_in_schema=False)
    async def billing_link(request: Request):
        if not service.settings.enabled:
            return error(request, 503)
        if not allowed(request):
            return error(request, 403)
        try:
            principal = await run_in_threadpool(service.resolve, request)
            if principal is None:
                return error(request, 401)
            token = await _input(request, "request_token")
            await run_in_threadpool(
                service.bind_billing, principal, token, subscriptions
            )
        except AuthInputError as exc:
            return error(request, exc.code)
        except CheckoutConflict:
            return error(request, 409)
        except ValueError:
            return error(request, 422)
        except CheckoutBusy:
            return error(request, 429)
        except CheckoutUnavailable:
            return error(request, 503)
        return JSONResponse(
            {"detail": "Powiązano rozliczenia i uzgodniono bieżący stan."},
            headers={"Cache-Control": "no-store"},
        )

    return router
