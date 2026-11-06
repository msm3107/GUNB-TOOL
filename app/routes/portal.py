"""Customer Portal entry: default deny, exact Origin, no caller-selected identifiers."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.identity import AuthenticatedCustomer, IdentityResolver
from app.portal_service import PortalForbidden, PortalService
from app.stripe_service import CheckoutBusy
from core.checkout_store import CheckoutUnavailable


def portal_router(
    service: PortalService, identity_resolver: IdentityResolver
) -> APIRouter:
    router = APIRouter()

    def failure(request, code):
        headers = {
            "Cache-Control": "no-store",
            **({"Retry-After": "60"} if code in (429, 503) else {}),
        }
        if request.headers.get("accept", "").lower().startswith("text/html"):
            return RedirectResponse(
                "/panel?notice=portal-error", status_code=303, headers=headers
            )
        return JSONResponse(
            {
                "detail": "Portal niedostępny"
                if code in (429, 503)
                else "Brak uprawnień lub niepoprawne żądanie"
            },
            status_code=code,
            headers=headers,
        )

    @router.post("/panel/portal", include_in_schema=False)
    async def portal(request: Request):
        if not service.settings.test_mode:
            return failure(request, 503)
        try:
            principal = await run_in_threadpool(identity_resolver, request)
        except CheckoutUnavailable:
            return failure(request, 503)
        if not isinstance(principal, AuthenticatedCustomer):
            return failure(request, 401)
        if request.headers.get("origin") != service.settings.origin:
            return failure(request, 403)
        async for chunk in request.stream():
            if chunk:
                return failure(
                    request, 400
                )  # no email/customer/order/return_url or other input accepted
        try:
            url = await run_in_threadpool(service.start, principal)
        except PortalForbidden:
            return failure(request, 403)
        except CheckoutBusy:
            return failure(request, 429)
        except CheckoutUnavailable:
            return failure(request, 503)
        return RedirectResponse(
            url, status_code=303, headers={"Cache-Control": "no-store"}
        )

    return router
