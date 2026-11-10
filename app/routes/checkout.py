"""Bounded test Checkout; no customer access or payment confirmation."""

import json
from typing import Literal
from urllib.parse import parse_qsl
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.auth import AuthService
from app.stripe_service import CheckoutBusy, CheckoutService
from core.checkout_store import CheckoutConflict, CheckoutUnavailable
from core.customer_dashboard import _scope
from core.places import PlaceCatalog

from .orders import BODY_LIMIT, EstimateInput, InputError, read_input


class CheckoutInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    kind: str = Field(max_length=20)
    request_token: str = Field(min_length=36, max_length=36)
    filters: EstimateInput


class OwnedCheckoutInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    kind: Literal["jednorazowy", "abonament"]
    request_token: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    filters: EstimateInput


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate field")
    return result


async def _owned_input(request, native):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > BODY_LIMIT:
            raise InputError("Zbyt duży formularz", 413)
        raw.extend(chunk)
    if native:
        data = _unique(parse_qsl(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=3))
        if set(data) != {"kind", "request_token", "filters"}:
            raise ValueError
        data["filters"] = json.loads(data["filters"], object_pairs_hook=_unique)
    else:
        data = json.loads(raw, object_pairs_hook=_unique)
    return OwnedCheckoutInput.model_validate(data)


def checkout_router(service: CheckoutService, templates, places: PlaceCatalog | None = None, auth: AuthService | None = None) -> APIRouter:
    router = APIRouter()

    def failure(message, code):
        return JSONResponse({"detail": message}, status_code=code,
                            headers={"Cache-Control": "no-store", "Retry-After": "60"} if code in (429, 503) else {"Cache-Control": "no-store"})

    def owned_page(request, selection=None, *, status=200, message="", login=False):
        filters_json = json.dumps(selection.filters.model_dump(), ensure_ascii=True, separators=(",", ":")) if selection else ""
        return templates.TemplateResponse(request=request, name="checkout_summary.html", context={
            "page_title": "Testowe zamówienie", "selection": selection, "filters_json": filters_json,
            "scope": _scope(filters_json), "message": message, "login": login,
            "offers": {"jednorazowy": service.settings.package_offer, "abonament": service.settings.subscription_offer},
        }, status_code=status, headers={"Cache-Control": "no-store", **({"Retry-After": "60"} if status in (429, 503) else {})})

    @router.post("/zamowienie/podsumowanie", include_in_schema=False)
    async def prepare(request: Request):
        if not service.settings.test_mode or auth is None or not auth.settings.enabled:
            return failure("Zamówienia online nie są jeszcze dostępne", 503)
        if request.headers.get("origin") != service.settings.origin:
            return failure("Niedozwolone źródło żądania", 403)
        selection = None
        try:
            filters = await read_input(request, native=True)
            selection = OwnedCheckoutInput(kind="jednorazowy", request_token=str(uuid4()), filters=filters)
            owner = await run_in_threadpool(auth.resolve, request)
            if owner is None:
                return owned_page(request, selection, status=401, message="Zaloguj się w nowej karcie, wróć tutaj i ponów tę próbę.", login=True)
            await run_in_threadpool(filters.filters, places)
            return owned_page(request, selection)
        except InputError as exc:
            return owned_page(request, selection, status=exc.status, message=str(exc))
        except CheckoutUnavailable:
            return owned_page(request, selection, status=503, message="Nie udało się sprawdzić konta. Zachowano tę próbę; ponów ją później.")

    async def owned_checkout(request, native):
        if auth is None or not auth.settings.enabled:
            return failure("Ta ścieżka wymaga konta i testowego Checkout", 503)
        selection = None
        def reject(message, code, *, login=False):
            return owned_page(request, selection, status=code, message=message, login=login) if native else failure(message, code)
        try:
            selection = await _owned_input(request, native)
            owner = await run_in_threadpool(auth.resolve, request)
            if owner is None:
                return reject("Zaloguj się w nowej karcie, wróć tutaj i ponów tę samą próbę.", 401, login=True)
            filters = await run_in_threadpool(selection.filters.filters, places)
            url = await run_in_threadpool(service.start_owned, customer_id=owner.customer_id,
                kind=selection.kind, filters=filters, request_token=selection.request_token)
            return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})
        except InputError as exc:
            return reject(str(exc), exc.status)
        except CheckoutConflict:
            return reject("Dane lub termin tej próby zmieniły się. Sprawdź panel przed rozpoczęciem nowego zamówienia.", 409)
        except CheckoutBusy:
            return reject("Zbyt wiele prób. Zachowano dane; ponów tę samą próbę później.", 429)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return reject("Niepoprawne dane. Wróć do konfiguratora i sprawdź wybór.", 422)
        except CheckoutUnavailable:
            return reject("Nie znamy wyniku tej próby. Zachowano dane; ponów tę samą próbę. Nie rozpoczynaj nowego zamówienia.", 503)

    @router.post("/zamowienie/checkout", include_in_schema=False)
    async def checkout(request: Request):
        if not service.settings.test_mode:
            return failure("Zamówienia online nie są jeszcze dostępne", 503)
        if request.headers.get("origin") != service.settings.origin:
            return failure("Niedozwolone źródło żądania", 403)
        media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media == "application/x-www-form-urlencoded" or (auth is not None and auth.settings.enabled):
            if media not in {"application/x-www-form-urlencoded", "application/json"}:
                return failure("Nieobsługiwany format danych", 415)
            return await owned_checkout(request, media == "application/x-www-form-urlencoded")
        try:
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise InputError("Nieobsługiwany format danych", 415)
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > BODY_LIMIT:
                    raise InputError("Zbyt duże żądanie", 413)
                body.extend(chunk)
            selection = CheckoutInput.model_validate_json(body)
            filters = await run_in_threadpool(selection.filters.filters, places)
            url = await run_in_threadpool(service.start, email=selection.email, kind=selection.kind,
                filters=filters, request_token=selection.request_token)
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
