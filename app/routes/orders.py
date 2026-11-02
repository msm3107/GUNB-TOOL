"""Package preview only: bounded form/JSON inputs, no order creation or checkout."""

from __future__ import annotations

from dataclasses import asdict
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app.estimator import EstimateUnavailable, Estimator
from core.models import LEAD_CATEGORIES
from core.package_selection import PackageFilters
from core.ranking import CATEGORY_PLURALS
from core.stages import TRADES
from core.teryt import VOIVODESHIPS

BODY_LIMIT = 8192
UNAVAILABLE = "Estymator jest chwilowo niedostępny. Spróbuj ponownie później."


class EstimateInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    wojewodztwo: str = Field(max_length=2)
    branza: str = Field(max_length=30)
    powiaty: list[str] = Field(default_factory=list, max_length=20)
    kategorie: list[str] = Field(default_factory=lambda: list(LEAD_CATEGORIES), max_length=7)
    lat: float | None = None
    lon: float | None = None
    promien_km: float | None = None

    def filters(self) -> PackageFilters:
        return PackageFilters(self.wojewodztwo, self.branza, tuple(self.powiaty), tuple(self.kategorie),
                              self.lat, self.lon, self.promien_km)


class InputError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


async def read_input(request: Request, *, native: bool = False) -> EstimateInput:
    expected = "application/x-www-form-urlencoded" if native else "application/json"
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != expected:
        raise InputError("Nieobsługiwany format danych", 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > BODY_LIMIT:
            raise InputError("Zbyt duży formularz", 413)
        body.extend(chunk)
    try:
        if not native:
            return EstimateInput.model_validate_json(body)
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=40, errors="strict")
        values: dict[str, object] = {}
        for name, items in parsed.items():
            if name in {"powiaty", "kategorie"}:
                values[name] = items
            elif len(items) != 1:
                raise ValueError("Pole wystąpiło wielokrotnie")
            elif name in {"lat", "lon", "promien_km"}:
                values[name] = float(items[0]) if items[0] else None
            else:
                values[name] = items[0]
        values.setdefault("kategorie", [])
        return EstimateInput.model_validate(values)
    except (ValueError, ValidationError) as exc:
        raise InputError("Sprawdź województwo, branżę, typy inwestycji oraz współrzędne") from exc


def order_router(estimator: Estimator, templates) -> APIRouter:
    router = APIRouter()

    async def page(request: Request, *, selection=None, result=None, error=None, status=200):
        selection = selection or EstimateInput(wojewodztwo="28", branza="materialy")
        county_warning = None
        try:
            counties = await run_in_threadpool(estimator.counties)
        except EstimateUnavailable:
            counties = ()
            county_warning = "Lista powiatów jest chwilowo niedostępna. Zachowano wybrane powiaty; możesz ponowić obliczenie."
        # Metadata may fail (or change) after a successful estimate. Never let a
        # re-render silently widen a submitted county scope to the whole region.
        known = {code for code, _ in counties}
        counties += tuple((code, f"Powiat {code}") for code in selection.powiaty
                          if code not in known and len(code) == 4 and code.isascii()
                          and code.isdigit() and code.startswith(selection.wojewodztwo))
        return templates.TemplateResponse(request=request, name="order_configurator.html", status_code=status,
            headers={"Cache-Control": "no-store"}, context={
                "page_title": "Konfigurator paczki", "counties": counties, "voivodeships": VOIVODESHIPS,
                "trades": TRADES, "categories": CATEGORY_PLURALS,
                "selection": selection, "county_warning": county_warning,
                "estimate": result, "error": error,
            })

    @router.get("/zamowienie", name="order_configurator", include_in_schema=False)
    async def show(request: Request):
        return await page(request)

    @router.post("/zamowienie", include_in_schema=False)
    async def native_estimate(request: Request):
        selection = None
        try:
            selection = await read_input(request, native=True)
            result = await run_in_threadpool(estimator.estimate, selection.filters())
            return await page(request, selection=selection, result=result)
        except InputError as exc:
            return await page(request, selection=selection, error=str(exc), status=exc.status)
        except ValueError as exc:
            return await page(request, selection=selection, error=str(exc), status=422)
        except EstimateUnavailable:
            return await page(request, selection=selection, error=UNAVAILABLE, status=503)

    @router.post("/api/estimate", name="package_estimate", include_in_schema=False)
    async def estimate(request: Request):
        try:
            selection = await read_input(request)
            result = await run_in_threadpool(estimator.estimate, selection.filters())
            return JSONResponse(asdict(result), headers={"Cache-Control": "no-store"})
        except InputError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=exc.status, headers={"Cache-Control": "no-store"})
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422, headers={"Cache-Control": "no-store"})
        except EstimateUnavailable:
            return JSONResponse({"detail": UNAVAILABLE}, status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "2"})

    return router
