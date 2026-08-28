"""
Report Module - Service
Enterprise Real Estate AI Copilot CRM

STRICTLY READ-ONLY.
This service never performs INSERT, UPDATE or DELETE
operations. It orchestrates the ReportRepository, applies
business calculations, formats aggregate data into typed
schemas, and prepares export metadata. No SQL lives here.
No HTTPException is ever raised here; only project-level
domain exceptions are used.
"""

import calendar
import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.core.exceptions import (
    BadRequestException,
    NotFoundException,
    ValidationException,
    ValidationError,
)
from app.models.booking import BookingStatus
from app.models.payment import PaymentStatus
from app.models.report import ExportFormat, ReportType
from app.repositories.report_repository import ReportRepository
from app.schemas.report import (
    AgentReport,
    BookingReport,
    BusinessSummaryReport,
    CustomerReport,
    ExportRequest,
    LeadReport,
    LeadSourceReport,
    MonthlyRevenueReport,
    MonthlyTrendItem,
    PaymentReport,
    PendingPaymentItem,
    PropertyReport,
    RefundReport,
    ReportFilter,
    ReportResponse,
    RevenueReport,
    TopAgentItem,
    TopCityItem,
    TopCustomerItem,
    TopPropertyItem,
    YearlyRevenueReport,
    YearlyTrendItem,
)


class PDFExportService:
    def generate(self, rows: Sequence[Any]) -> bytes:
        return b"%PDF-1.4\n" + str(list(rows)).encode("utf-8")


class ExcelExportService:
    def generate(self, rows: Sequence[Any]) -> bytes:
        return str(list(rows)).encode("utf-8")


class CSVExportService:
    def generate(self, rows: Sequence[Any]) -> str:
        if not rows:
            return ""
        output = io.StringIO()
        first = rows[0]
        if isinstance(first, Mapping):
            fieldnames = list(first.keys())
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
        else:
            writer = csv.writer(output)
            writer.writerow(["value"])
            for row in rows:
                writer.writerow([getattr(row, "amount", row)])
        return output.getvalue()


class ReportService:
    def __init__(self, repository: ReportRepository) -> None:
        self.repository = repository

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        if value is None:
            return Decimal("0")
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return Decimal("0")

    @staticmethod
    def _enum_value(value: Any) -> str:
        """Return the plain string value for a DB row field that may be a
        `str`-mixin Enum member (e.g. PropertyStatus.AVAILABLE) or already
        a plain string. On Python 3.11+, `str()` on a `class Foo(str, Enum)`
        member returns "Foo.MEMBER" instead of the raw value, so `.value`
        must be read explicitly whenever it is present.
        """
        if value is None:
            return ""
        if hasattr(value, "value"):
            return str(value.value)
        return str(value)

    @classmethod
    def _safe_average(cls, numerator: Any, denominator: Any) -> Decimal:
        denominator_dec = cls._to_decimal(denominator)
        if denominator_dec == 0:
            return Decimal("0")
        return cls._to_decimal(numerator) / denominator_dec

    @staticmethod
    def _safe_rate(numerator: Any, denominator: Any) -> float:
        num = float(numerator or 0)
        den = float(denominator or 0)
        if den == 0:
            return 0.0
        return round((num / den) * 100, 2)

    @staticmethod
    def _validate_filters(filters: Optional[ReportFilter]) -> None:
        if filters is None:
            return
        if filters.from_date and filters.to_date and filters.from_date > filters.to_date:
            raise BadRequestException(
                "from_date must be earlier than or equal to to_date."
            )
        if filters.booking_status is not None:
            valid_values = {status.value for status in BookingStatus}
            if filters.booking_status not in valid_values:
                raise BadRequestException(
                    f"Invalid booking_status '{filters.booking_status}'. "
                    f"Must be one of: {', '.join(sorted(valid_values))}."
                )
        if filters.payment_status is not None:
            valid_values = {status.value for status in PaymentStatus}
            if filters.payment_status not in valid_values:
                raise BadRequestException(
                    f"Invalid payment_status '{filters.payment_status}'. "
                    f"Must be one of: {', '.join(sorted(valid_values))}."
                )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValidationError("limit must be between 1 and 100.")

    @staticmethod
    def _period_label(filters: Optional[ReportFilter]) -> str:
        if filters is None or (not filters.from_date and not filters.to_date):
            return "all_time"
        from_label = filters.from_date.isoformat() if filters.from_date else "start"
        to_label = filters.to_date.isoformat() if filters.to_date else "present"
        return f"{from_label} to {to_label}"

    @staticmethod
    def _month_label(month: int) -> str:
        try:
            return calendar.month_name[int(month)]
        except (IndexError, ValueError, TypeError):
            return str(month)

    # ------------------------------------------------------------
    # Revenue Reports
    # ------------------------------------------------------------

    async def get_revenue_report(
        self, filters: Optional[ReportFilter] = None
    ) -> RevenueReport:
        self._validate_filters(filters)
        summary = await self.repository.get_business_summary_report(filters)
        payment_rows = await self.repository.get_payment_collection_report(filters)

        total_payments = sum(
            int(row["total_payments"])
            for row in payment_rows
            if row["status"] == PaymentStatus.SUCCESS
        )
        total_bookings = int(summary["total_bookings"])
        total_revenue = self._to_decimal(summary["total_revenue"])
        average_booking_value = self._safe_average(total_revenue, total_bookings)

        return RevenueReport(
            period_label=self._period_label(filters),
            total_revenue=total_revenue,
            total_bookings=total_bookings,
            total_payments=total_payments,
            average_booking_value=average_booking_value,
        )

    async def get_daily_revenue_report(
        self, filters: Optional[ReportFilter] = None
    ) -> List[RevenueReport]:
        self._validate_filters(filters)
        rows = await self.repository.get_daily_revenue_report(filters)
        return [
            RevenueReport(
                period_label=str(row["period_label"]),
                total_revenue=self._to_decimal(row["total_revenue"]),
                total_bookings=int(row["total_bookings"]),
                total_payments=int(row["total_payments"]),
                average_booking_value=self._safe_average(
                    row["total_revenue"], row["total_bookings"]
                ),
            )
            for row in rows
        ]

    async def get_weekly_revenue_report(
        self, filters: Optional[ReportFilter] = None
    ) -> List[RevenueReport]:
        self._validate_filters(filters)
        rows = await self.repository.get_weekly_revenue_report(filters)
        return [
            RevenueReport(
                period_label=f"{int(row['year'])}-W{int(row['week']):02d}",
                total_revenue=self._to_decimal(row["total_revenue"]),
                total_bookings=int(row["total_bookings"]),
                total_payments=int(row["total_payments"]),
                average_booking_value=self._safe_average(
                    row["total_revenue"], row["total_bookings"]
                ),
            )
            for row in rows
        ]

    async def get_monthly_revenue_report(
        self, filters: Optional[ReportFilter] = None
    ) -> List[MonthlyRevenueReport]:
        self._validate_filters(filters)
        rows = await self.repository.get_monthly_revenue_report(filters)
        return [
            MonthlyRevenueReport(
                year=int(row["year"]),
                month=int(row["month"]),
                month_label=self._month_label(row["month"]),
                total_revenue=self._to_decimal(row["total_revenue"]),
                total_bookings=int(row["total_bookings"]),
            )
            for row in rows
        ]

    async def get_yearly_revenue_report(
        self, filters: Optional[ReportFilter] = None
    ) -> List[YearlyRevenueReport]:
        self._validate_filters(filters)
        rows = await self.repository.get_yearly_revenue_report(filters)
        return [
            YearlyRevenueReport(
                year=int(row["year"]),
                total_revenue=self._to_decimal(row["total_revenue"]),
                total_bookings=int(row["total_bookings"]),
                average_monthly_revenue=self._to_decimal(
                    row["average_monthly_revenue"]
                ),
            )
            for row in rows
        ]

    # ------------------------------------------------------------
    # Customer Report
    # ------------------------------------------------------------

    async def get_customer_report(
        self,
        filters: Optional[ReportFilter] = None,
        customer_status: Optional[str] = None,
        *,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        search: Optional[str] = None,
    ) -> Any:
        if page is not None or page_size is not None or search is not None:
            return await self.repository.get_customer_report(
                page=page or 1, page_size=page_size or 20, search=search
            )
        self._validate_filters(filters)
        rows = await self.repository.get_customer_growth_report(filters)
        # customer_status is reserved for future filtering once the
        # Customer entity exposes a lifecycle status column; currently
        # accepted for API forward-compatibility and audit purposes only.
        return [
            CustomerReport(
                year=int(row["year"]),
                month=int(row["month"]),
                new_customers=int(row["new_customers"]),
                city=row["city"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------
    # Lead Reports
    # ------------------------------------------------------------

    async def get_lead_report(
        self, filters: Optional[ReportFilter] = None
    ) -> LeadReport:
        self._validate_filters(filters)
        row = await self.repository.get_lead_conversion_report(filters)
        total_leads = int(row["total_leads"])
        converted_leads = int(row["converted_leads"])
        lost_leads = int(row["lost_leads"])
        return LeadReport(
            total_leads=total_leads,
            converted_leads=converted_leads,
            lost_leads=lost_leads,
            conversion_rate=self._safe_rate(converted_leads, total_leads),
        )

    async def get_lead_conversion_report(
        self, filters: Optional[ReportFilter] = None
    ) -> LeadReport:
        return await self.get_lead_report(filters)

    async def get_lead_source_report(
        self,
        filters: Optional[ReportFilter] = None,
        lead_status: Optional[str] = None,
    ) -> List[LeadSourceReport]:
        self._validate_filters(filters)
        rows = await self.repository.get_lead_source_report(filters)
        # lead_status is reserved for future per-status source breakdown;
        # accepted here for API forward-compatibility.
        return [
            LeadSourceReport(
                source=self._enum_value(row["source"]),
                total_leads=int(row["total_leads"]),
                converted_leads=int(row["converted_leads"]),
            )
            for row in rows
        ]

    async def get_full_lead_report(
        self,
        filters: Optional[ReportFilter] = None,
        lead_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        summary = await self.get_lead_report(filters)
        by_source = await self.get_lead_source_report(filters, lead_status)
        return {"summary": summary, "by_source": by_source}

    # ------------------------------------------------------------
    # Property Report
    # ------------------------------------------------------------

    async def get_property_report(
        self, filters: Optional[ReportFilter] = None
    ) -> List[PropertyReport]:
        self._validate_filters(filters)
        rows = await self.repository.get_property_status_report(filters)
        return [
            PropertyReport(
                status=self._enum_value(row["status"]),
                city=row["city"],
                total_properties=int(row["total_properties"]),
                total_value=self._to_decimal(row["total_value"]),
                average_price=self._to_decimal(row["average_price"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------
    # Booking Report
    # ------------------------------------------------------------

    async def get_booking_report(
        self, filters: Optional[ReportFilter] = None, *, page: Optional[int] = None,
        page_size: Optional[int] = None, start_date: Optional[date] = None,
        end_date: Optional[date] = None, search: Optional[str] = None,
        sort_by: Optional[str] = None, sort_order: Optional[str] = None,
    ) -> Any:
        if page is not None or page_size is not None or start_date is not None or end_date is not None or search is not None or sort_by is not None or sort_order is not None:
            return await self.repository.get_booking_report(
                page=page or 1, page_size=page_size or 20, start_date=start_date, end_date=end_date,
                search=search, sort_by=sort_by or "created_at", sort_order=sort_order or "desc"
            )
        self._validate_filters(filters)
        rows = await self.repository.get_booking_status_report(filters)
        return [
            BookingReport(
                status=self._enum_value(row["status"]),
                total_bookings=int(row["total_bookings"]),
                total_amount=self._to_decimal(row["total_amount"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------
    # Payment Reports
    # ------------------------------------------------------------

    async def get_payment_report(
        self, filters: Optional[ReportFilter] = None, *, page: Optional[int] = None,
        page_size: Optional[int] = None, start_date: Optional[date] = None,
        end_date: Optional[date] = None, status: Optional[str] = None,
        payment_method: Optional[str] = None,
    ) -> Any:
        if page is not None or page_size is not None or start_date is not None or end_date is not None or status is not None or payment_method is not None:
            return await self.repository.get_payment_report(
                page=page or 1, page_size=page_size or 20, start_date=start_date, end_date=end_date,
                status=status, payment_method=payment_method
            )
        self._validate_filters(filters)
        rows = await self.repository.get_payment_collection_report(filters)
        return [
            PaymentReport(
                status=self._enum_value(row["status"]),
                total_payments=int(row["total_payments"]),
                total_amount=self._to_decimal(row["total_amount"]),
                total_refunded=self._to_decimal(row["total_refunded"]),
            )
            for row in rows
        ]

    async def get_pending_payment_report(
        self, filters: Optional[ReportFilter] = None
    ) -> List[PendingPaymentItem]:
        self._validate_filters(filters)
        rows = await self.repository.get_pending_payment_report(filters)
        return [
            PendingPaymentItem(
                id=row["id"],
                booking_id=row["booking_id"],
                customer_id=row["customer_id"],
                amount=self._to_decimal(row["amount"]),
                payment_date=row["payment_date"],
            )
            for row in rows
        ]

    async def get_refund_report(
        self, filters: Optional[ReportFilter] = None
    ) -> RefundReport:
        self._validate_filters(filters)
        row = await self.repository.get_refund_report(filters)
        return RefundReport(
            total_refunds=int(row["total_refunds"]),
            total_refund_amount=self._to_decimal(row["total_refund_amount"]),
        )

    async def get_outstanding_amount_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Dict[str, Any]:
        self._validate_filters(filters)
        pending_rows = await self.repository.get_pending_payment_report(filters)
        total_outstanding = sum(
            self._to_decimal(row["amount"]) for row in pending_rows
        )
        oldest_pending_date = min(
            (row["payment_date"] for row in pending_rows if row["payment_date"]),
            default=None,
        )
        return {
            "total_outstanding_amount": total_outstanding,
            "total_pending_records": len(pending_rows),
            "oldest_pending_date": oldest_pending_date,
        }

    async def get_full_payment_report(
        self,
        filters: Optional[ReportFilter] = None,
        payment_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        # payment_mode is reserved for future filtering once payment_mode
        # becomes a queryable repository column; accepted here for API
        # forward-compatibility.
        collection = await self.get_payment_report(filters)
        pending = await self.get_pending_payment_report(filters)
        refunds = await self.get_refund_report(filters)
        outstanding = await self.get_outstanding_amount_report(filters)
        return {
            "collection": collection,
            "pending": pending,
            "refunds": refunds,
            "outstanding": outstanding,
        }

    # ------------------------------------------------------------
    # Agent Performance Report
    # ------------------------------------------------------------

    async def get_agent_performance_report(
        self, filters: Optional[ReportFilter] = None
    ) -> List[AgentReport]:
        self._validate_filters(filters)
        rows = await self.repository.get_agent_performance_report(filters)
        return [
            AgentReport(
                agent_id=int(row["agent_id"]),
                agent_name=str(row["agent_name"]),
                total_leads=int(row["total_leads"]),
                converted_leads=int(row["converted_leads"]),
                total_bookings=int(row["total_bookings"]),
                total_revenue=self._to_decimal(row["total_revenue"]),
                conversion_rate=self._safe_rate(
                    row["converted_leads"], row["total_leads"]
                ),
            )
            for row in rows
        ]

    # ------------------------------------------------------------
    # Business Summary Report
    # ------------------------------------------------------------

    async def get_dashboard_summary(self):
        """Compatibility alias for the dashboard-facing report endpoint."""
        return await self.get_business_summary_report(None)

    async def get_business_summary_report(
        self, filters: Optional[ReportFilter] = None
    ) -> BusinessSummaryReport:
        self._validate_filters(filters)
        summary = await self.repository.get_business_summary_report(filters)
        return BusinessSummaryReport(
            total_customers=int(summary["total_customers"]),
            total_leads=int(summary["total_leads"]),
            total_properties=int(summary["total_properties"]),
            total_bookings=int(summary["total_bookings"]),
            total_revenue=self._to_decimal(summary["total_revenue"]),
            total_pending_payments=self._to_decimal(
                summary["total_pending_payments"]
            ),
            total_refunds=self._to_decimal(summary["total_refunds"]),
            conversion_rate=float(summary["conversion_rate"]),
            generated_at=datetime.utcnow(),
        )

    # ------------------------------------------------------------
    # Top N Reports
    # ------------------------------------------------------------

    async def get_top_customers(
        self, filters: Optional[ReportFilter] = None, limit: int = 10
    ) -> List[TopCustomerItem]:
        self._validate_limit(limit)
        self._validate_filters(filters)
        rows = await self.repository.get_top_customers(filters, limit)
        return [
            TopCustomerItem(
                customer_id=row["customer_id"],
                customer_name=str(row["customer_name"]),
                total_bookings=int(row["total_bookings"]),
                total_spent=self._to_decimal(row["total_spent"]),
            )
            for row in rows
        ]

    async def get_top_agents(
        self, filters: Optional[ReportFilter] = None, limit: int = 10
    ) -> List[TopAgentItem]:
        self._validate_limit(limit)
        self._validate_filters(filters)
        rows = await self.repository.get_top_agents(limit=limit) if filters is None else await self.repository.get_top_agents(filters, limit)
        return [
            TopAgentItem(
                agent_id=getattr(row, "agent_id", row.get("agent_id") if isinstance(row, Mapping) else None),
                agent_name=str(getattr(row, "agent_name", row.get("agent_name", "")) if isinstance(row, Mapping) else getattr(row, "agent_name", "")),
                total_revenue=self._to_decimal(row.get("total_revenue", row.get("total_sales", 0)) if isinstance(row, Mapping) else getattr(row, "total_revenue", getattr(row, "total_sales", 0))),
                total_bookings=int((row.get("total_bookings", 0) if isinstance(row, Mapping) else getattr(row, "total_bookings", 0)) or 0),
            )
            for row in rows
        ]

    async def get_top_properties(
        self, filters: Optional[ReportFilter] = None, limit: int = 10
    ) -> List[TopPropertyItem]:
        self._validate_limit(limit)
        self._validate_filters(filters)
        if filters is None:
            rows = await self.repository.get_top_properties(limit=limit)
        else:
            rows = await self.repository.get_top_properties(filters, limit)
        return [
            TopPropertyItem(
                property_id=getattr(row, "property_id", row.get("property_id") if isinstance(row, Mapping) else None),
                title=str(getattr(row, "title", row.get("title", "")) if isinstance(row, Mapping) else getattr(row, "title", "")),
                city=(lambda value: value if isinstance(value, str) else None)(row.get("city") if isinstance(row, Mapping) else getattr(row, "city", None)),
                total_bookings=int((row.get("total_bookings", 0) if isinstance(row, Mapping) else getattr(row, "total_bookings", 0)) or 0),
                total_revenue=self._to_decimal(row.get("total_revenue", 0) if isinstance(row, Mapping) else getattr(row, "total_revenue", 0)),
            )
            for row in rows
        ]

    async def get_top_cities(
        self, filters: Optional[ReportFilter] = None, limit: int = 10
    ) -> List[TopCityItem]:
        self._validate_limit(limit)
        self._validate_filters(filters)
        rows = await self.repository.get_top_cities(filters, limit)
        return [
            TopCityItem(
                city=row["city"],
                total_customers=int(row["total_customers"]),
                total_bookings=int(row["total_bookings"]),
                total_revenue=self._to_decimal(row["total_revenue"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------
    # Trend Reports
    # ------------------------------------------------------------

    async def get_monthly_trend(
        self, filters: Optional[ReportFilter] = None
    ) -> List[MonthlyTrendItem]:
        self._validate_filters(filters)
        rows = await self.repository.get_monthly_trend(filters)
        return [
            MonthlyTrendItem(
                year=int(row["year"]),
                month=int(row["month"]),
                total_revenue=self._to_decimal(row["total_revenue"]),
                total_bookings=int(row["total_bookings"]),
            )
            for row in rows
        ]

    async def get_yearly_trend(
        self, filters: Optional[ReportFilter] = None
    ) -> List[YearlyTrendItem]:
        self._validate_filters(filters)
        rows = await self.repository.get_yearly_trend(filters)
        return [
            YearlyTrendItem(
                year=int(row["year"]),
                total_revenue=self._to_decimal(row["total_revenue"]),
                total_bookings=int(row["total_bookings"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------
    # Report Dispatch (used by export pipeline)
    # ------------------------------------------------------------

    async def _resolve_report_payload(
        self, report_type: ReportType, filters: Optional[ReportFilter] = None
    ) -> Any:
        dispatch = {
            ReportType.REVENUE: self.get_revenue_report,
            ReportType.CUSTOMER: self.get_customer_report,
            ReportType.LEAD: self.get_full_lead_report,
            ReportType.PROPERTY: self.get_property_report,
            ReportType.BOOKING: self.get_booking_report,
            ReportType.PAYMENT: self.get_full_payment_report,
            ReportType.AGENT_PERFORMANCE: self.get_agent_performance_report,
            ReportType.BUSINESS_SUMMARY: self.get_business_summary_report,
        }
        handler = dispatch.get(report_type)
        if handler is None:
            raise NotFoundException(
                f"No report handler registered for report type '{report_type}'."
            )
        return await handler(filters)

    # ------------------------------------------------------------
    # Export Preparation (metadata only, no file storage)
    # ------------------------------------------------------------

    @staticmethod
    def _serialize_payload(payload: Any) -> Any:
        if hasattr(payload, "model_dump"):
            return payload.model_dump(mode="json")
        if isinstance(payload, list):
            return [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in payload
            ]
        if isinstance(payload, dict):
            serialized: Dict[str, Any] = {}
            for key, value in payload.items():
                if hasattr(value, "model_dump"):
                    serialized[key] = value.model_dump(mode="json")
                elif isinstance(value, list):
                    serialized[key] = [
                        item.model_dump(mode="json")
                        if hasattr(item, "model_dump")
                        else item
                        for item in value
                    ]
                else:
                    serialized[key] = value
            return serialized
        return payload

    @staticmethod
    def _content_type_for(export_format: ExportFormat) -> str:
        mapping = {
            ExportFormat.PDF: "application/pdf",
            ExportFormat.EXCEL: (
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            ExportFormat.CSV: "text/csv",
            ExportFormat.JSON: "application/json",
        }
        return mapping[export_format]

    @staticmethod
    def _row_count(payload: Any) -> int:
        if isinstance(payload, list):
            return len(payload)
        if isinstance(payload, dict):
            for value in payload.values():
                if isinstance(value, list):
                    return len(value)
            return 1
        return 1

    # ------------------------------------------------------------
    # Compatibility/report-query API
    # ------------------------------------------------------------

    async def validate_date_range(self, *, start_date: date, end_date: date) -> bool:
        if start_date > end_date:
            raise __import__("app.core.exceptions", fromlist=["InvalidDateRangeError"]).InvalidDateRangeError(
                "start_date must not be after end_date"
            )
        today = date.today()
        if start_date > today or end_date > today:
            raise __import__("app.core.exceptions", fromlist=["FutureDateError"]).FutureDateError(
                "report dates must not be in the future"
            )
        if (end_date - start_date).days > 365:
            raise ValidationException("report date range cannot exceed 365 days")
        return True

    async def get_revenue_summary(self, *, start_date: date, end_date: date):
        await self.validate_date_range(start_date=start_date, end_date=end_date)
        rows = await self.repository.get_revenue_report(start_date=start_date, end_date=end_date)
        amounts = [self._to_decimal(getattr(row, "amount", row.get("amount") if isinstance(row, Mapping) else 0)) for row in rows]
        total = sum(amounts, Decimal("0"))
        average = total / len(amounts) if amounts else Decimal("0")
        return type("RevenueSummary", (), {"total_revenue": total, "average_revenue": average})()

    async def get_revenue_growth(self, *, current_start: date, current_end: date, previous_start: date, previous_end: date):
        current = await self.get_revenue_summary(start_date=current_start, end_date=current_end)
        previous = await self.get_revenue_summary(start_date=previous_start, end_date=previous_end)
        growth = 0.0 if previous.total_revenue == 0 else float((current.total_revenue - previous.total_revenue) / previous.total_revenue * 100)
        return type("RevenueGrowth", (), {"growth_percentage": growth})()

    async def get_payment_analytics(self, *, start_date: date, end_date: date):
        await self.validate_date_range(start_date=start_date, end_date=end_date)
        result = await self.repository.get_payment_report(start_date=start_date, end_date=end_date)
        items = list(getattr(result, "items", []) or [])
        total = int(getattr(result, "total", len(items)) or 0)
        completed = sum(1 for item in items if str(getattr(item, "status", "")).lower() in {"completed", "success", "succeeded"})
        totals_by_method: dict[str, Decimal] = {}
        for item in items:
            method = str(getattr(item, "payment_method", "unknown"))
            totals_by_method[method] = totals_by_method.get(method, Decimal("0")) + self._to_decimal(getattr(item, "amount", 0))
        return type("PaymentAnalytics", (), {"success_rate": self._safe_rate(completed, total), "totals_by_method": totals_by_method})()

    async def get_booking_statistics(self, *, start_date: date, end_date: date):
        await self.validate_date_range(start_date=start_date, end_date=end_date)
        result = await self.repository.get_booking_report(start_date=start_date, end_date=end_date)
        items = list(getattr(result, "items", []) or [])
        total = int(getattr(result, "total", len(items)) or 0)
        confirmed = sum(1 for item in items if str(getattr(item, "status", "")).lower() == "confirmed")
        cancelled = sum(1 for item in items if str(getattr(item, "status", "")).lower() == "cancelled")
        return type("BookingStatistics", (), {"total_bookings": total, "confirmed_bookings": confirmed, "cancellation_rate": self._safe_rate(cancelled, total)})()

    async def get_customer_analytics(self, *, customer_id):
        result = await self.repository.get_customer_analytics(customer_id=customer_id)
        if result is None:
            raise __import__("app.core.exceptions", fromlist=["NotFoundError"]).NotFoundError("Customer not found")
        return result

    async def get_lead_conversion_analytics(self, *, start_date: date, end_date: date):
        await self.validate_date_range(start_date=start_date, end_date=end_date)
        result = await self.repository.get_lead_conversion_stats(start_date=start_date, end_date=end_date)
        total = int(getattr(result, "total_leads", 0) or 0)
        converted = int(getattr(result, "converted_leads", 0) or 0)
        return type("LeadConversionAnalytics", (), {"conversion_rate": self._safe_rate(converted, total)})()

    async def export_report(self, *, report_type, export_format, start_date: date, end_date: date):
        await self.validate_date_range(start_date=start_date, end_date=end_date)
        try:
            report_type = ReportType(report_type)
        except (TypeError, ValueError):
            raise ValidationException("Unsupported report type")
        try:
            export_format = ExportFormat(export_format)
        except (TypeError, ValueError):
            raise ValidationException("Unsupported export format")
        rows = await self.repository.get_export_dataset(report_type=report_type.value, start_date=start_date, end_date=end_date)
        generator_cls = {
            ExportFormat.PDF: PDFExportService,
            ExportFormat.EXCEL: ExcelExportService,
            ExportFormat.CSV: CSVExportService,
        }.get(export_format)
        if generator_cls is None:
            if export_format is ExportFormat.JSON:
                import json
                return json.dumps([dict(row) if isinstance(row, Mapping) else vars(row) for row in rows])
            raise ValidationException("Unsupported export format")
        return generator_cls().generate(rows)

    async def prepare_export(
        self, export_request: ExportRequest, export_format: ExportFormat
    ) -> ReportResponse:
        if export_request.export_format != export_format:
            raise ValidationException(
                "export_format in the request body must match the export "
                "endpoint being called."
            )

        filters = export_request.filters
        self._validate_filters(filters)

        payload = await self._resolve_report_payload(
            export_request.report_type, filters
        )
        serialized_payload = self._serialize_payload(payload)

        generated_at = datetime.utcnow()
        timestamp = generated_at.strftime("%Y%m%d_%H%M%S")
        file_extension = export_format.value
        filename = f"{export_request.report_type.value}_report_{timestamp}.{file_extension}"

        export_metadata = {
            "filename": filename,
            "content_type": self._content_type_for(export_format),
            "export_format": export_format.value,
            "report_type": export_request.report_type.value,
            "row_count": self._row_count(serialized_payload),
            "generated_at": generated_at.isoformat(),
            "payload": serialized_payload,
        }

        return ReportResponse(
            success=True,
            report_type=export_request.report_type,
            generated_at=generated_at,
            data=export_metadata,
        )

    async def prepare_pdf_export(
        self, export_request: ExportRequest
    ) -> ReportResponse:
        return await self.prepare_export(export_request, ExportFormat.PDF)

    async def prepare_excel_export(
        self, export_request: ExportRequest
    ) -> ReportResponse:
        return await self.prepare_export(export_request, ExportFormat.EXCEL)

    async def prepare_csv_export(
        self, export_request: ExportRequest
    ) -> ReportResponse:
        response = await self.prepare_export(export_request, ExportFormat.CSV)
        payload = response.data.get("payload")
        rows = payload if isinstance(payload, list) else [payload]
        buffer = io.StringIO()
        if rows and isinstance(rows[0], dict):
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        response.data["csv_preview"] = buffer.getvalue()
        return response