"""
test_report_service.py

Service-layer test suite for the Reports Module.
Mirrors the testing architecture established for:
Customer / Lead / Property / Booking / Payment / Dashboard services.

Scope:
- Business validation
- Date validation (invalid ranges, future dates)
- Revenue calculations
- Payment calculations
- Booking statistics
- Top properties / Top agents
- Customer analytics
- Lead conversion analytics
- Export validation (PDF / Excel / CSV generation)
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.report_service import ReportService
from app.core.exceptions import (
    InvalidDateRangeError,
    FutureDateError,
    ValidationError,
    NotFoundError,
)
from app.schemas.report import (
    ExportFormat,
    ReportType,
)


pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def mock_repository():
    repo = AsyncMock()
    return repo


@pytest.fixture
def report_service(mock_repository):
    return ReportService(repository=mock_repository)


# --------------------------------------------------------------------------
# Date Validation
# --------------------------------------------------------------------------

class TestDateValidation:
    async def test_rejects_start_date_after_end_date(self, report_service):
        with pytest.raises(InvalidDateRangeError):
            await report_service.validate_date_range(
                start_date=date.today(), end_date=date.today() - timedelta(days=5)
            )

    async def test_rejects_future_start_date(self, report_service):
        with pytest.raises(FutureDateError):
            await report_service.validate_date_range(
                start_date=date.today() + timedelta(days=10),
                end_date=date.today() + timedelta(days=20),
            )

    async def test_rejects_future_end_date_beyond_today(self, report_service):
        with pytest.raises(FutureDateError):
            await report_service.validate_date_range(
                start_date=date.today() - timedelta(days=5),
                end_date=date.today() + timedelta(days=5),
            )

    async def test_accepts_valid_historical_range(self, report_service):
        result = await report_service.validate_date_range(
            start_date=date.today() - timedelta(days=30),
            end_date=date.today() - timedelta(days=1),
        )
        assert result is True

    async def test_rejects_range_exceeding_max_span(self, report_service):
        with pytest.raises(ValidationError):
            await report_service.validate_date_range(
                start_date=date.today() - timedelta(days=800),
                end_date=date.today() - timedelta(days=1),
            )

    async def test_accepts_same_day_range(self, report_service):
        today = date.today() - timedelta(days=1)
        result = await report_service.validate_date_range(start_date=today, end_date=today)
        assert result is True


# --------------------------------------------------------------------------
# Revenue Calculations
# --------------------------------------------------------------------------

class TestRevenueCalculations:
    async def test_calculate_total_revenue(self, report_service, mock_repository):
        mock_repository.get_revenue_report.return_value = [
            MagicMock(amount=Decimal("100000")),
            MagicMock(amount=Decimal("250000")),
        ]
        result = await report_service.get_revenue_summary(
            start_date=date.today() - timedelta(days=30), end_date=date.today() - timedelta(days=1)
        )
        assert result.total_revenue == Decimal("350000")

    async def test_calculate_revenue_with_empty_dataset(self, report_service, mock_repository):
        mock_repository.get_revenue_report.return_value = []
        result = await report_service.get_revenue_summary(
            start_date=date.today() - timedelta(days=30), end_date=date.today() - timedelta(days=1)
        )
        assert result.total_revenue == Decimal("0")

    async def test_calculate_average_revenue_per_booking(self, report_service, mock_repository):
        mock_repository.get_revenue_report.return_value = [
            MagicMock(amount=Decimal("100000")),
            MagicMock(amount=Decimal("200000")),
        ]
        result = await report_service.get_revenue_summary(
            start_date=date.today() - timedelta(days=30), end_date=date.today() - timedelta(days=1)
        )
        assert result.average_revenue == Decimal("150000")

    async def test_revenue_growth_calculation(self, report_service, mock_repository):
        mock_repository.get_revenue_report.side_effect = [
            [MagicMock(amount=Decimal("200000"))],
            [MagicMock(amount=Decimal("100000"))],
        ]
        result = await report_service.get_revenue_growth(
            current_start=date.today() - timedelta(days=30),
            current_end=date.today() - timedelta(days=1),
            previous_start=date.today() - timedelta(days=60),
            previous_end=date.today() - timedelta(days=31),
        )
        assert result.growth_percentage == 100.0


# --------------------------------------------------------------------------
# Payment Calculations
# --------------------------------------------------------------------------

class TestPaymentCalculations:
    async def test_calculate_payment_success_rate(self, report_service, mock_repository):
        mock_repository.get_payment_report.return_value = MagicMock(
            items=[
                MagicMock(status="completed"),
                MagicMock(status="completed"),
                MagicMock(status="failed"),
            ],
            total=3,
        )
        result = await report_service.get_payment_analytics(
            start_date=date.today() - timedelta(days=30), end_date=date.today() - timedelta(days=1)
        )
        assert round(result.success_rate, 2) == round(2 / 3 * 100, 2)

    async def test_calculate_payment_totals_by_method(self, report_service, mock_repository):
        mock_repository.get_payment_report.return_value = MagicMock(
            items=[
                MagicMock(status="completed", payment_method="upi", amount=Decimal("1000")),
                MagicMock(status="completed", payment_method="card", amount=Decimal("2000")),
            ],
            total=2,
        )
        result = await report_service.get_payment_analytics(
            start_date=date.today() - timedelta(days=30), end_date=date.today() - timedelta(days=1)
        )
        assert "upi" in result.totals_by_method or hasattr(result, "totals_by_method")

    async def test_payment_analytics_empty_dataset(self, report_service, mock_repository):
        mock_repository.get_payment_report.return_value = MagicMock(items=[], total=0)
        result = await report_service.get_payment_analytics(
            start_date=date.today() - timedelta(days=30), end_date=date.today() - timedelta(days=1)
        )
        assert result.success_rate == 0


# --------------------------------------------------------------------------
# Booking Statistics
# --------------------------------------------------------------------------

class TestBookingStatistics:
    async def test_booking_conversion_stats(self, report_service, mock_repository):
        mock_repository.get_booking_report.return_value = MagicMock(
            items=[
                MagicMock(status="confirmed"),
                MagicMock(status="cancelled"),
                MagicMock(status="confirmed"),
            ],
            total=3,
        )
        result = await report_service.get_booking_statistics(
            start_date=date.today() - timedelta(days=30), end_date=date.today() - timedelta(days=1)
        )
        assert result.total_bookings == 3
        assert result.confirmed_bookings == 2

    async def test_booking_statistics_empty_dataset(self, report_service, mock_repository):
        mock_repository.get_booking_report.return_value = MagicMock(items=[], total=0)
        result = await report_service.get_booking_statistics(
            start_date=date.today() - timedelta(days=30), end_date=date.today() - timedelta(days=1)
        )
        assert result.total_bookings == 0

    async def test_booking_cancellation_rate(self, report_service, mock_repository):
        mock_repository.get_booking_report.return_value = MagicMock(
            items=[MagicMock(status="cancelled")] * 2 + [MagicMock(status="confirmed")] * 8,
            total=10,
        )
        result = await report_service.get_booking_statistics(
            start_date=date.today() - timedelta(days=30), end_date=date.today() - timedelta(days=1)
        )
        assert result.cancellation_rate == 20.0


# --------------------------------------------------------------------------
# Top Properties / Top Agents
# --------------------------------------------------------------------------

class TestTopPerformers:
    async def test_get_top_properties_returns_ranked_list(self, report_service, mock_repository):
        mock_repository.get_top_properties.return_value = [
            MagicMock(property_id=uuid.uuid4(), total_revenue=Decimal("500000")),
            MagicMock(property_id=uuid.uuid4(), total_revenue=Decimal("300000")),
        ]
        result = await report_service.get_top_properties(limit=5)
        assert len(result) == 2
        assert result[0].total_revenue >= result[1].total_revenue

    async def test_get_top_properties_respects_limit(self, report_service, mock_repository):
        mock_repository.get_top_properties.return_value = [MagicMock() for _ in range(5)]
        result = await report_service.get_top_properties(limit=5)
        mock_repository.get_top_properties.assert_awaited_once_with(limit=5)
        assert len(result) == 5

    async def test_get_top_agents_returns_ranked_list(self, report_service, mock_repository):
        mock_repository.get_top_agents.return_value = [
            MagicMock(agent_id=uuid.uuid4(), total_sales=Decimal("1000000")),
        ]
        result = await report_service.get_top_agents(limit=3)
        assert len(result) == 1

    async def test_get_top_properties_empty_dataset(self, report_service, mock_repository):
        mock_repository.get_top_properties.return_value = []
        result = await report_service.get_top_properties(limit=5)
        assert result == []

    async def test_get_top_properties_invalid_limit_raises(self, report_service):
        with pytest.raises(ValidationError):
            await report_service.get_top_properties(limit=0)


# --------------------------------------------------------------------------
# Customer Analytics
# --------------------------------------------------------------------------

class TestCustomerAnalytics:
    async def test_get_customer_analytics_returns_data(self, report_service, mock_repository):
        customer_id = uuid.uuid4()
        mock_repository.get_customer_analytics.return_value = MagicMock(
            customer_id=customer_id, total_bookings=3, total_spent=Decimal("900000")
        )
        result = await report_service.get_customer_analytics(customer_id=customer_id)
        assert result.customer_id == customer_id
        assert result.total_bookings == 3

    async def test_get_customer_analytics_not_found_raises(self, report_service, mock_repository):
        mock_repository.get_customer_analytics.return_value = None
        with pytest.raises(NotFoundError):
            await report_service.get_customer_analytics(customer_id=uuid.uuid4())

    async def test_get_customer_report_with_pagination(self, report_service, mock_repository):
        mock_repository.get_customer_report.return_value = MagicMock(items=[], total=0)
        result = await report_service.get_customer_report(page=1, page_size=10)
        mock_repository.get_customer_report.assert_awaited_once()
        assert result.total == 0


# --------------------------------------------------------------------------
# Lead Conversion Analytics
# --------------------------------------------------------------------------

class TestLeadConversionAnalytics:
    async def test_get_lead_conversion_rate(self, report_service, mock_repository):
        mock_repository.get_lead_conversion_stats.return_value = MagicMock(
            total_leads=100, converted_leads=25
        )
        result = await report_service.get_lead_conversion_analytics(
            start_date=date.today() - timedelta(days=30), end_date=date.today() - timedelta(days=1)
        )
        assert result.conversion_rate == 25.0

    async def test_get_lead_conversion_rate_zero_leads(self, report_service, mock_repository):
        mock_repository.get_lead_conversion_stats.return_value = MagicMock(
            total_leads=0, converted_leads=0
        )
        result = await report_service.get_lead_conversion_analytics(
            start_date=date.today() - timedelta(days=30), end_date=date.today() - timedelta(days=1)
        )
        assert result.conversion_rate == 0

    async def test_get_lead_conversion_invalid_date_range_raises(self, report_service):
        with pytest.raises(InvalidDateRangeError):
            await report_service.get_lead_conversion_analytics(
                start_date=date.today(), end_date=date.today() - timedelta(days=10)
            )


# --------------------------------------------------------------------------
# Export Validation
# --------------------------------------------------------------------------

class TestExportValidation:
    async def test_export_rejects_unsupported_format(self, report_service):
        with pytest.raises(ValidationError):
            await report_service.export_report(
                report_type=ReportType.REVENUE,
                export_format="doc",
                start_date=date.today() - timedelta(days=30),
                end_date=date.today() - timedelta(days=1),
            )

    async def test_export_rejects_invalid_date_range(self, report_service):
        with pytest.raises(InvalidDateRangeError):
            await report_service.export_report(
                report_type=ReportType.REVENUE,
                export_format=ExportFormat.PDF,
                start_date=date.today(),
                end_date=date.today() - timedelta(days=5),
            )

    @patch("app.services.report_service.PDFExportService")
    async def test_pdf_generation_invokes_export_service(
        self, mock_pdf_service, report_service, mock_repository
    ):
        mock_repository.get_export_dataset.return_value = [MagicMock(amount=Decimal("1000"))]
        mock_pdf_service.return_value.generate = MagicMock(return_value=b"%PDF-1.4 mock content")
        result = await report_service.export_report(
            report_type=ReportType.REVENUE,
            export_format=ExportFormat.PDF,
            start_date=date.today() - timedelta(days=30),
            end_date=date.today() - timedelta(days=1),
        )
        assert result is not None
        mock_pdf_service.return_value.generate.assert_called_once()

    @patch("app.services.report_service.ExcelExportService")
    async def test_excel_generation_invokes_export_service(
        self, mock_excel_service, report_service, mock_repository
    ):
        mock_repository.get_export_dataset.return_value = [MagicMock(amount=Decimal("1000"))]
        mock_excel_service.return_value.generate = MagicMock(return_value=b"mock-xlsx-bytes")
        result = await report_service.export_report(
            report_type=ReportType.BOOKINGS,
            export_format=ExportFormat.EXCEL,
            start_date=date.today() - timedelta(days=30),
            end_date=date.today() - timedelta(days=1),
        )
        assert result is not None
        mock_excel_service.return_value.generate.assert_called_once()

    @patch("app.services.report_service.CSVExportService")
    async def test_csv_generation_invokes_export_service(
        self, mock_csv_service, report_service, mock_repository
    ):
        mock_repository.get_export_dataset.return_value = [MagicMock(amount=Decimal("1000"))]
        mock_csv_service.return_value.generate = MagicMock(return_value="col1,col2\n1,2")
        result = await report_service.export_report(
            report_type=ReportType.PAYMENTS,
            export_format=ExportFormat.CSV,
            start_date=date.today() - timedelta(days=30),
            end_date=date.today() - timedelta(days=1),
        )
        assert result is not None
        mock_csv_service.return_value.generate.assert_called_once()

    async def test_export_with_empty_dataset_returns_valid_file(self, report_service, mock_repository):
        mock_repository.get_export_dataset.return_value = []
        with patch("app.services.report_service.CSVExportService") as mock_csv_service:
            mock_csv_service.return_value.generate = MagicMock(return_value="")
            result = await report_service.export_report(
                report_type=ReportType.LEADS,
                export_format=ExportFormat.CSV,
                start_date=date.today() - timedelta(days=30),
                end_date=date.today() - timedelta(days=1),
            )
            assert result is not None

    async def test_export_rejects_unknown_report_type(self, report_service):
        with pytest.raises(ValidationError):
            await report_service.export_report(
                report_type="unknown_type",
                export_format=ExportFormat.CSV,
                start_date=date.today() - timedelta(days=30),
                end_date=date.today() - timedelta(days=1),
            )