"""
Report Module - Schemas
Enterprise Real Estate AI Copilot CRM

Read-only reporting DTOs. These schemas are strictly used
for serializing aggregated read data produced by the
ReportRepository. No schema in this module is ever used
for persistence.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.report import ExportFormat, ReportPeriod, ReportType


class ReportFilter(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_date: Optional[date] = None
    to_date: Optional[date] = None
    agent_id: Optional[int] = None
    city: Optional[str] = None
    property_type: Optional[str] = None
    booking_status: Optional[str] = None
    payment_status: Optional[str] = None
    period: Optional[ReportPeriod] = None


class RevenueReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period_label: str
    total_revenue: Decimal
    total_bookings: int
    total_payments: int
    average_booking_value: Decimal


class MonthlyRevenueReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    year: int
    month: int
    month_label: Optional[str] = None
    total_revenue: Decimal
    total_bookings: int


class YearlyRevenueReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    year: int
    total_revenue: Decimal
    total_bookings: int
    average_monthly_revenue: Decimal


class CustomerReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    year: int
    month: int
    new_customers: int
    city: Optional[str] = None


class LeadReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_leads: int
    converted_leads: int
    lost_leads: int
    conversion_rate: float


class LeadSourceReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source: str
    total_leads: int
    converted_leads: int


class PropertyReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: str
    city: Optional[str] = None
    total_properties: int
    total_value: Decimal
    average_price: Decimal


class BookingReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: str
    total_bookings: int
    total_amount: Decimal


class PaymentReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: str
    total_payments: int
    total_amount: Decimal
    total_refunded: Decimal


class PendingPaymentItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    booking_id: UUID
    customer_id: UUID
    amount: Decimal
    payment_date: Optional[datetime] = None


class RefundReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_refunds: int
    total_refund_amount: Decimal


class AgentReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_id: int
    agent_name: str
    total_leads: int
    converted_leads: int
    total_bookings: int
    total_revenue: Decimal
    conversion_rate: float


class BusinessSummaryReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_customers: int
    total_leads: int
    total_properties: int
    total_bookings: int
    total_revenue: Decimal
    total_pending_payments: Decimal
    total_refunds: Decimal
    conversion_rate: float
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class TopCustomerItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    customer_id: UUID
    customer_name: str
    total_bookings: int
    total_spent: Decimal


class TopAgentItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_id: UUID | int
    agent_name: str
    total_revenue: Decimal
    total_bookings: int


class TopPropertyItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    property_id: UUID | int
    title: str
    city: Optional[str] = None
    total_bookings: int
    total_revenue: Decimal


class TopCityItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    city: Optional[str] = None
    total_customers: int
    total_bookings: int
    total_revenue: Decimal


class MonthlyTrendItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    year: int
    month: int
    total_revenue: Decimal
    total_bookings: int


class YearlyTrendItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    year: int
    total_revenue: Decimal
    total_bookings: int


class ExportRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    report_type: ReportType
    export_format: ExportFormat
    filters: Optional[ReportFilter] = None


class ReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    success: bool = True
    report_type: ReportType
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    data: Any