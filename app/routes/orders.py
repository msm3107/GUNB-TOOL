"""Package preview only: bounded form/JSON inputs, no order creation or checkout."""

from __future__ import annotations

import math
from dataclasses import asdict
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError
from starlette.concurrency import run_in_threadpool

from app.estimator import EstimateUnavailable, Estimator
from core.models import LEAD_CATEGORIES
from core.package_selection import PackageFilterError, PackageFilters
from core.places import PlaceCatalog, PlaceUnavailable
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
    kategorie: list[str] = Field(
        default_factory=lambda: list(LEAD_CATEGORIES), max_length=7
    )
    lat: float | None = None
    lon: float | None = None
    promien_km: float | None = None
    place_query: str = Field(default="", max_length=80)
    place_id: str | None = Field(default=None, max_length=100)
    place_version: str | None = Field(default=None, max_length=128)
    _search_places: bool = PrivateAttr(default=False)

    def filters(self, places: PlaceCatalog | None = None) -> PackageFilters:
        lat, lon = self.lat, self.lon
        if self.place_id or self.place_version:
            if not self.place_id or not self.place_version:
                raise InputError(
                    "Wybierz miejscowość z aktualnych wyników",
                    field_errors={"place_id": "Wybierz miejscowość ponownie"},
                )
            if lat is not None or lon is not None:
                raise InputError(
                    "Wybierz miejscowość albo ręczne współrzędne",
                    field_errors={
                        "place_id": "Usuń ręczne współrzędne lub jawnie wyczyść miejscowość"
                    },
                )
            try:
                place = (places or PlaceCatalog(None)).get(
                    self.place_id, self.place_version
                )
            except PlaceUnavailable:
                raise InputError(
                    "Katalog miejscowości jest niedostępny. Zachowano wybór; ponów ręcznie.",
                    503,
                    field_errors={"place_id": "Katalog jest niedostępny"},
                ) from None
            if place is None:
                raise InputError(
                    "Wersja katalogu lub miejscowość zmieniła się. Wyszukaj i wybierz ponownie.",
                    field_errors={
                        "place_id": "Wybierz miejscowość z aktualnego katalogu"
                    },
                )
            lat, lon = place.lat, place.lon
        elif self.place_query.strip():
            raise InputError(
                "Wybierz konkretną miejscowość z wyników",
                field_errors={
                    "place_id": "Sama nazwa nie wybiera punktu; wyszukaj i wybierz miejscowość"
                },
            )
        try:
            return PackageFilters(
                self.wojewodztwo,
                self.branza,
                tuple(self.powiaty),
                tuple(self.kategorie),
                lat,
                lon,
                self.promien_km,
            )
        except PackageFilterError as exc:
            fields = (
                ("lat", "lon", "promien_km")
                if exc.field == "coordinates"
                else (exc.field,)
            )
            raise InputError(
                str(exc), field_errors={key: str(exc) for key in fields}
            ) from None


class InputError(ValueError):
    def __init__(
        self, message: str, status: int = 422, *, field_errors=None, selection=None
    ):
        super().__init__(message)
        self.status = status
        self.field_errors = field_errors or {}
        self.selection = selection


def display_selection(values):
    """Unvalidated bounded native values for HTML only; never used for estimation."""
    result = {}
    for key in EstimateInput.model_fields:
        value = values.get(key, [] if key in ("powiaty", "kategorie") else "")
        if key in ("powiaty", "kategorie"):
            result[key] = (
                [v[:512] for v in value[:40] if isinstance(v, str)]
                if isinstance(value, list)
                else []
            )
        else:
            result[key] = value[:512] if isinstance(value, str) else ""
    return result


async def read_input(request: Request, *, native: bool = False) -> EstimateInput:
    expected = "application/x-www-form-urlencoded" if native else "application/json"
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != expected
    ):
        raise InputError("Nieobsługiwany format danych", 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > BODY_LIMIT:
            raise InputError("Zbyt duży formularz", 413)
        body.extend(chunk)
    selection = None
    try:
        if not native:
            return EstimateInput.model_validate_json(body)
        parsed = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=40,
            errors="strict",
        )
        action = parsed.pop("place_action", ["estimate"])
        if action not in (["estimate"], ["search"]):
            raise ValueError("Niepoprawna akcja formularza")
        selection = display_selection(
            {
                key: items if key in ("powiaty", "kategorie") else items[0]
                for key, items in parsed.items()
            }
        )
        values: dict[str, object] = {}
        number_errors = {}
        for name, items in parsed.items():
            if name in {"powiaty", "kategorie"}:
                values[name] = items
            elif len(items) != 1:
                raise ValueError("Pole wystąpiło wielokrotnie")
            elif name in {"lat", "lon", "promien_km"}:
                try:
                    values[name] = float(items[0]) if items[0] else None
                    if values[name] is not None and not math.isfinite(values[name]):
                        number_errors[name] = "Podaj skończoną liczbę"
                except ValueError:
                    number_errors[name] = "Podaj poprawną liczbę"
            elif name in {"place_id", "place_version"}:
                values[name] = items[0] or None
            else:
                values[name] = items[0]
        if number_errors:
            raise InputError(
                "Sprawdź współrzędne i promień",
                field_errors=number_errors,
                selection=selection,
            )
        values.setdefault("kategorie", [])
        selected = EstimateInput.model_validate(values)
        selected._search_places = action == ["search"]
        return selected
    except InputError:
        raise
    except ValidationError as exc:
        fields = {
            (
                "place_id" if e["loc"][0] == "place_version" else str(e["loc"][0])
            ): "Sprawdź wartość pola"
            for e in exc.errors()
            if e["loc"] and e["loc"][0] in EstimateInput.model_fields
        }
        raise InputError(
            "Sprawdź województwo, branżę, typy inwestycji oraz współrzędne",
            field_errors=fields,
            selection=selection,
        ) from None
    except (ValueError, UnicodeError):
        raise InputError("Sprawdź format formularza", selection=selection) from None


def order_router(
    estimator: Estimator, templates, places: PlaceCatalog | None = None
) -> APIRouter:
    router = APIRouter()
    places = places or PlaceCatalog(None)

    async def page(
        request: Request,
        *,
        selection=None,
        result=None,
        error=None,
        status=200,
        field_errors=None,
        place_results=None,
    ):
        selection = (
            selection
            if selection is not None
            else (
                display_selection({})
                if error
                else EstimateInput(wojewodztwo="28", branza="materialy")
            )
        )
        if isinstance(selection, EstimateInput):
            selection = selection.model_dump()
        selected_place = None
        if selection.get("place_id") and selection.get("place_version"):
            try:
                selected_place = await run_in_threadpool(
                    places.get, selection["place_id"], selection["place_version"]
                )
            except (PlaceUnavailable, ValueError):
                pass  # Keep the exact ID/version even when metadata cannot be read.
        county_warning = None
        try:
            counties = await run_in_threadpool(estimator.counties)
        except EstimateUnavailable:
            counties = ()
            county_warning = "Lista powiatów jest chwilowo niedostępna. Zachowano wybrane powiaty; możesz ponowić obliczenie."
        # Metadata may fail (or change) after a successful estimate. Never let a
        # re-render silently widen a submitted county scope to the whole region.
        known = {code for code, _ in counties}
        counties += tuple(
            (code, f"Powiat {code} — zachowany wybór")
            for code in selection["powiaty"]
            if code not in known
        )
        categories = dict(CATEGORY_PLURALS)
        for key in selection["kategorie"]:
            categories.setdefault(key, "Nieznany typ — odznacz, aby poprawić wybór")
        return templates.TemplateResponse(
            request=request,
            name="order_configurator.html",
            status_code=status,
            headers={"Cache-Control": "no-store"},
            context={
                "page_title": "Konfigurator paczki",
                "counties": counties,
                "voivodeships": VOIVODESHIPS,
                "trades": TRADES,
                "categories": categories,
                "selection": selection,
                "county_warning": county_warning,
                "estimate": result,
                "error": error,
                "field_errors": field_errors or {},
                "place_results": place_results,
                "selected_place": selected_place,
            },
        )

    @router.get("/zamowienie", name="order_configurator", include_in_schema=False)
    async def show(request: Request):
        return await page(request)

    @router.post("/zamowienie", include_in_schema=False)
    async def native_estimate(request: Request):
        selection = None
        try:
            selection = await read_input(request, native=True)
            if selection._search_places:
                try:
                    results = await run_in_threadpool(
                        places.search, selection.place_query, selection.wojewodztwo
                    )
                except ValueError as exc:
                    raise InputError(
                        str(exc), field_errors={"place_query": str(exc)}
                    ) from None
                view = selection.model_dump()
                view.update(
                    place_id=None,
                    place_version=results[0].source_version if results else None,
                )
                return await page(request, selection=view, place_results=results)
            filters = await run_in_threadpool(selection.filters, places)
            result = await run_in_threadpool(estimator.estimate, filters)
            return await page(request, selection=selection, result=result)
        except InputError as exc:
            return await page(
                request,
                selection=exc.selection if exc.selection is not None else selection,
                error=str(exc),
                status=exc.status,
                field_errors=exc.field_errors,
            )
        except ValueError as exc:
            return await page(request, selection=selection, error=str(exc), status=422)
        except EstimateUnavailable:
            return await page(
                request, selection=selection, error=UNAVAILABLE, status=503
            )
        except PlaceUnavailable:
            return await page(
                request,
                selection=selection,
                error="Katalog miejscowości jest niedostępny. Zachowano filtry.",
                status=503,
            )

    @router.post("/api/estimate", name="package_estimate", include_in_schema=False)
    async def estimate(request: Request):
        try:
            selection = await read_input(request)
            filters = await run_in_threadpool(selection.filters, places)
            result = await run_in_threadpool(estimator.estimate, filters)
            return JSONResponse(asdict(result), headers={"Cache-Control": "no-store"})
        except InputError as exc:
            return JSONResponse(
                {
                    "detail": str(exc),
                    **({"field_errors": exc.field_errors} if exc.field_errors else {}),
                },
                status_code=exc.status,
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as exc:
            return JSONResponse(
                {"detail": str(exc)},
                status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        except EstimateUnavailable:
            return JSONResponse(
                {"detail": UNAVAILABLE},
                status_code=503,
                headers={"Cache-Control": "no-store", "Retry-After": "2"},
            )

    return router
