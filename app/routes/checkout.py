"""Bounded test Checkout; no customer access or payment confirmation."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.stripe_service import CheckoutBusy, CheckoutService
from core.checkout_store import CheckoutConflict, CheckoutUnavailable

from .orders import BODY_LIMIT, EstimateInput, InputError


class CheckoutInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    kind: str = Field(max_length=20)
    request_token: str = Field(min_length=36, max_length=36)
    filters: EstimateInput


def checkout_router(service: CheckoutService, templates) -> APIRouter:
    router = APIRouter()

    def failure(message, code):
        return JSONResponse({"detail": message}, status_code=code,
                            headers={"Cache-Control": "no-store", "Retry-After": "60"} if code in (429, 503) else {"Cache-Control": "no-store"})

    @router.post("/zamowienie/checkout", include_in_schema=False)
    async def checkout(request: Request):
        if not service.settings.test_mode:
            return failure("Zamówienia online nie są jeszcze dostępne", 503)
        if request.headers.get("origin") != service.settings.origin:
            return failure("Niedozwolone źródło żądania", 403)
        try:
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise InputError("Nieobsługiwany format danych", 415)
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > BODY_LIMIT:
                    raise InputError("Zbyt duże żądanie", 413)
                body.extend(chunk)
            selection = CheckoutInput.model_validate_json(body)
            url = await run_in_threadpool(service.start, email=selection.email, kind=selection.kind,
                filters=selection.filters.filters(), request_token=selection.request_token)
            return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})
        except InputError as exc:
            return failure(str(exc), exc.status)
        except CheckoutConflict:
            return failure("Próba wygasła lub identyfikator został użyty dla innych danych", 409)
        except CheckoutBusy:
            return failure("Zbyt wiele prób. Spróbuj ponownie później z tym samym identyfikatorem", 429)
        except (ValidationError, ValueError):
            return failure("Sprawdź adres e-mail, rodzaj paczki, identyfikator i filtry", 422)
        except CheckoutUnavailable:
            return failure("Testowy Checkout jest chwilowo niedostępny. Ponów z tym samym identyfikatorem", 503)

    @router.get("/zamowienie/sukces", include_in_schema=False)
    async def returned(request: Request):
        # Query string and browser navigation prove nothing about payment or
        # identity. Only a future verified webhook can change payment state.
        return templates.TemplateResponse(request=request, name="checkout_return.html",
            context={"page_title": "Powrót z testowego Checkout"}, headers={"Cache-Control": "no-store"})

    return router
