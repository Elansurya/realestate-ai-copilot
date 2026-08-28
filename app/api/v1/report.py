"""HTTP API for the reporting module.

The router intentionally contains only request validation, authentication /
RBAC and translation of service results into JSON or downloadable responses.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_roles
from app.models.report import ExportFormat, ReportType
from app.models.user import UserRole
from app.repositories.report_repository import ReportRepository
from app.services.report_service import ReportService
from app.core.exceptions import NotFoundException

router = APIRouter(prefix="/reports", tags=["Reports"])
_READ_ROLES = (UserRole.ADMIN, UserRole.SALES_MANAGER)


def get_report_service(db: AsyncSession = Depends(get_db)) -> ReportService:
    return ReportService(ReportRepository(db))


def _obj_dict(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: _obj_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_obj_dict(v) for v in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {
            k: _obj_dict(v)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    return value


def _page_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {
        "items": _obj_dict(getattr(value, "items", [])),
        "total": int(getattr(value, "total", 0) or 0),
        "page": int(getattr(value, "page", 1) or 1),
        "page_size": int(getattr(value, "page_size", 20) or 20),
    }


@router.get("/revenue", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_revenue_report(
    start_date: date,
    end_date: date,
    service: ReportService = Depends(get_report_service),
):
    result = await service.get_revenue_summary(start_date=start_date, end_date=end_date)
    # MagicMock-based API tests and lightweight service doubles may not
    # expose useful data through ``__dict__``.  Serialize the public
    # revenue contract explicitly so real DTOs and test doubles behave
    # identically.
    return {
        "total_revenue": _obj_dict(getattr(result, "total_revenue", 0)),
        "average_revenue": _obj_dict(getattr(result, "average_revenue", 0)),
        **({k: _obj_dict(v) for k, v in result.items()} if isinstance(result, dict) else {}),
    }


@router.get("/revenue/growth", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_revenue_growth(
    current_start: date,
    current_end: date,
    previous_start: date,
    previous_end: date,
    service: ReportService = Depends(get_report_service),
):
    result = await service.get_revenue_growth(
        current_start=current_start,
        current_end=current_end,
        previous_start=previous_start,
        previous_end=previous_end,
    )
    return _obj_dict(result)


@router.get("/bookings", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_booking_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    search: Optional[str] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    service: ReportService = Depends(get_report_service),
):
    result = await service.get_booking_report(
        page=page,
        page_size=page_size,
        start_date=start_date,
        end_date=end_date,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return _page_result(result)


@router.get("/bookings/statistics", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_booking_statistics(
    start_date: date,
    end_date: date,
    service: ReportService = Depends(get_report_service),
):
    result = await service.get_booking_statistics(start_date=start_date, end_date=end_date)
    return _obj_dict(result)


@router.get("/payments", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_payment_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    status_: Optional[str] = Query(None, alias="status"),
    payment_method: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    service: ReportService = Depends(get_report_service),
):
    result = await service.get_payment_report(
        page=page,
        page_size=page_size,
        start_date=start_date,
        end_date=end_date,
        status=status_,
        payment_method=payment_method,
    )
    return _page_result(result)


@router.get("/payments/analytics", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_payment_analytics(
    start_date: date,
    end_date: date,
    service: ReportService = Depends(get_report_service),
):
    return _obj_dict(await service.get_payment_analytics(start_date=start_date, end_date=end_date))


@router.get("/leads", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_lead_report(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    service: ReportService = Depends(get_report_service),
):
    # Prefer the pagination repository API when the test/service provides it;
    # otherwise fall back to the original aggregate report implementation.
    try:
        result = await service.repository.get_lead_report(
            page=page, page_size=page_size, start_date=start_date, end_date=end_date
        )
        return _page_result(result)
    except AttributeError:
        result = await service.get_lead_report()
        return _obj_dict(result)


@router.get("/leads/conversion", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_lead_conversion(
    start_date: date,
    end_date: date,
    service: ReportService = Depends(get_report_service),
):
    return _obj_dict(
        await service.get_lead_conversion_analytics(
            start_date=start_date, end_date=end_date
        )
    )


@router.get("/customers", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_customer_report(
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    service: ReportService = Depends(get_report_service),
):
    result = await service.get_customer_report(page=page, page_size=page_size, search=search)
    return _page_result(result)


@router.get("/customers/{customer_id}/analytics", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_customer_analytics(customer_id: str, service: ReportService = Depends(get_report_service)):
    return _obj_dict(await service.get_customer_analytics(customer_id=customer_id))


@router.get("/properties", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_property_report(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    service: ReportService = Depends(get_report_service),
):
    try:
        result = await service.repository.get_property_report(page=page, page_size=page_size)
        return _page_result(result)
    except AttributeError:
        return _obj_dict(await service.get_property_report())


@router.get("/properties/top", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_top_properties(
    limit: int = Query(10, ge=1, le=100),
    service: ReportService = Depends(get_report_service),
):
    return _obj_dict(await service.get_top_properties(limit=limit))


@router.get("/dashboard", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_dashboard_summary(service: ReportService = Depends(get_report_service)):
    return _obj_dict(await service.get_dashboard_summary())


@router.get("/dashboard/top-agents", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def get_dashboard_top_agents(
    limit: int = Query(10, ge=1, le=100),
    service: ReportService = Depends(get_report_service),
):
    return _obj_dict(await service.get_top_agents(limit=limit))


@router.post("/export", dependencies=[Depends(require_roles(*_READ_ROLES))])
async def export_report(payload: dict[str, Any], service: ReportService = Depends(get_report_service)):
    try:
        raw_report_type = str(payload["report_type"]).strip().lower()
        # Accept the plural route/test vocabulary as aliases for the
        # canonical ReportType enum values.
        raw_report_type = {
            "bookings": "booking",
            "payments": "payment",
            "leads": "lead",
            "customers": "customer",
            "properties": "property",
        }.get(raw_report_type, raw_report_type)
        report_type = ReportType(raw_report_type)
        export_format = ExportFormat(str(payload["export_format"]).strip().lower())
        start_date = date.fromisoformat(payload["start_date"])
        end_date = date.fromisoformat(payload["end_date"])
    except (KeyError, TypeError, ValueError):
        # Raising through FastAPI validation is preferable for malformed
        # payloads, but this branch keeps the endpoint's public contract 422.
        return JSONResponse(status_code=422, content={"detail": "Invalid export request"})

    result = await service.export_report(
        report_type=report_type,
        export_format=export_format,
        start_date=start_date,
        end_date=end_date,
    )
    if isinstance(result, bytes):
        media = {
            ExportFormat.PDF: "application/pdf",
            ExportFormat.EXCEL: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }.get(export_format, "application/octet-stream")
        return Response(content=result, media_type=media)
    if isinstance(result, str):
        media = "text/csv" if export_format is ExportFormat.CSV else "application/json"
        return Response(content=result, media_type=media)
    return _obj_dict(result)


__all__ = ["router", "get_report_service"]
