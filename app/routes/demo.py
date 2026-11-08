"""Public, fixed fictional sample; no customer database, filesystem paths or writes."""

from functools import lru_cache
from io import BytesIO
from typing import Literal

from fastapi import APIRouter, Request, Response

from core.demo import DEMO_PACKAGE_DATE, DEMO_PACKAGE_TRADE, demo_package_items
from core.export_package import write_csv, write_xlsx
from core.models import DeliveryCategory
from core.ranking import stage_note
from core.scoring import score_investment
from core.stages import get_trade

Format = Literal["xlsx", "csv"]
LABELS = {
    DeliveryCategory.NEW: "Nowe",
    DeliveryCategory.CHANGE: "Zmiany",
    DeliveryCategory.FULL: "Pełna baza",
}
MEDIA = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
}


@lru_cache(maxsize=2)
def _artifact(kind: Format) -> bytes:
    with BytesIO() as output:
        writer = write_xlsx if kind == "xlsx" else write_csv
        writer(
            demo_package_items(),
            output,
            trade_key=DEMO_PACKAGE_TRADE,
            as_of=DEMO_PACKAGE_DATE,
        )
        return output.getvalue()


def demo_router(templates) -> APIRouter:
    router = APIRouter()

    @router.get("/przyklad", name="demo_package", include_in_schema=False)
    def preview(request: Request):
        trade = get_trade(DEMO_PACKAGE_TRADE)
        assert trade is not None
        rows = [
            {
                "item": item,
                "label": LABELS[item.category],
                "score": score_investment(item.investment),
                "timing": stage_note(trade, item.investment, DEMO_PACKAGE_DATE),
            }
            for item in demo_package_items()
        ]
        return templates.TemplateResponse(
            request=request,
            name="demo_package.html",
            context={
                "page_title": "Fikcyjna próbka paczki",
                "rows": rows,
                "sample_date": DEMO_PACKAGE_DATE,
            },
            headers={"Cache-Control": "no-store"},
        )

    def download(kind: Format, request: Request):
        payload = _artifact(kind)
        return Response(
            payload if request.method == "GET" else b"",
            media_type=MEDIA[kind],
            headers={
                "Content-Disposition": f'attachment; filename="zolta-tablica-przyklad-dane-fikcyjne.{kind}"',
                "Content-Length": str(len(payload)),
                "Cache-Control": "no-store",
            },
        )

    @router.api_route(
        "/przyklad.xlsx",
        methods=["GET", "HEAD"],
        name="demo_xlsx",
        include_in_schema=False,
    )
    def xlsx(request: Request):
        return download("xlsx", request)

    @router.api_route(
        "/przyklad.csv",
        methods=["GET", "HEAD"],
        name="demo_csv",
        include_in_schema=False,
    )
    def csv(request: Request):
        return download("csv", request)

    return router
