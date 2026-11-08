"""Bounded reads of an independently published local catalog; no geocoder."""

from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from core.places import PlaceCatalog, PlaceUnavailable


def places_router(catalog: PlaceCatalog) -> APIRouter:
    router = APIRouter()

    @router.get("/api/places", name="place_search", include_in_schema=False)
    async def search(request: Request):
        headers = {"Cache-Control": "no-store"}
        try:
            pairs = request.query_params.multi_items()
            if any(k not in {"q", "region"} for k, _ in pairs) or len(pairs) != len(
                dict(pairs)
            ):
                raise ValueError("Sprawdź parametry wyszukiwania")
            places = await run_in_threadpool(
                catalog.search,
                request.query_params.get("q", ""),
                request.query_params.get("region") or None,
            )
            return JSONResponse(
                {"places": [asdict(p) for p in places]}, headers=headers
            )
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422, headers=headers)
        except PlaceUnavailable:
            return JSONResponse(
                {
                    "detail": "Katalog miejscowości jest niedostępny. Ponów wyszukiwanie lub użyj współrzędnych zaawansowanych."
                },
                status_code=503,
                headers=headers,
            )

    return router
