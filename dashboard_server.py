from datetime import date
import hmac
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, UUID4

from reservations import MAX_PARTY_SIZE, ReservationBook, SplitOffer


DIRECTORY = Path(__file__).resolve().parent / "dashboard"
PAGES_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


class ManualBooking(BaseModel):
    request_id: UUID4
    day: date
    time: str = Field(pattern=r"^[0-9]{2}:[0-9]{2}$")
    people: int = Field(ge=1, le=MAX_PARTY_SIZE)
    contact: str = Field(default="", max_length=254)


class ManualSplit(BaseModel):
    request_id: UUID4
    offer_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    groups: list[Annotated[int, Field(ge=1, le=4)]] = Field(min_length=2, max_length=12)


class RescheduleBooking(BaseModel):
    request_id: UUID4
    day: date
    time: str = Field(pattern=r"^[0-9]{2}:[0-9]{2}$")


def _has_key(request: Request, key: str | None) -> bool:
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    return bool(key and scheme.lower() == "bearer" and hmac.compare_digest(supplied.encode("utf-8"), key.encode("utf-8")))


def _management_response(result: str | SplitOffer) -> JSONResponse:
    if isinstance(result, SplitOffer):
        return JSONResponse({"message": result.text, "offer_id": result.offer_id, "options": result.options}, headers=PAGES_HEADERS)
    return JSONResponse({"message": result}, headers=PAGES_HEADERS)


def mount_dashboard(
    app: FastAPI, book: ReservationBook, access_key: str | None, site_origin: str | None, management_key: str | None,
) -> None:
    dashboard = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    if site_origin:
        dashboard.add_middleware(
            CORSMiddleware, allow_origins=[site_origin], allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    if management_key == access_key:
        management_key = None

    def require_manager(request: Request):
        if not _has_key(request, management_key):
            raise HTTPException(status_code=403, detail="請使用店家管理連結。", headers=PAGES_HEADERS)

    @dashboard.get("/")
    def dashboard_page():
        return FileResponse(DIRECTORY / "index.html", headers=PAGES_HEADERS)

    @dashboard.get("/reservations")
    def daily_reservations(day: date, request: Request):
        can_manage = _has_key(request, management_key)
        if not can_manage and not _has_key(request, access_key):
            return JSONResponse({"detail": "View link required"}, status_code=401, headers=PAGES_HEADERS)
        return JSONResponse({"date": day.isoformat(), "reservations": book.reservations_for_day(day),
                             "can_manage": can_manage}, headers=PAGES_HEADERS)

    @dashboard.post("/manage/manual", dependencies=[Depends(require_manager)])
    def add_manual(booking: ManualBooking):
        return _management_response(book.add_manual(str(booking.request_id), booking.day, booking.time, booking.people, booking.contact))

    @dashboard.post("/manage/manual/choose", dependencies=[Depends(require_manager)])
    def choose_manual_split(selection: ManualSplit):
        return _management_response(book.choose_split(f"manual:{selection.request_id}", selection.offer_id, tuple(selection.groups)))

    @dashboard.post("/manage/reservations/{reference}/cancel", dependencies=[Depends(require_manager)])
    def cancel_booking(reference: str):
        return _management_response(book.cancel(reference))

    @dashboard.post("/manage/reservations/{reference}/reschedule", dependencies=[Depends(require_manager)])
    def move_booking(reference: str, change: RescheduleBooking):
        return _management_response(book.reschedule(reference, change.day.isoformat(), change.time, f"manual:{change.request_id}"))

    dashboard.mount("/assets", StaticFiles(directory=DIRECTORY / "assets"), name="dashboard_assets")
    app.mount("/dashboard", dashboard, name="dashboard")
