from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.dashboard import ActivityType, TrendPeriod


class RevenueSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_revenue: Decimal = Field(default=Decimal("0"))
    collected_revenue: Decimal = Field(default=Decimal("0"))
    pending_revenue: Decimal = Field(default=Decimal("0"))
    refunded_revenue: Decimal = Field(default=Decimal("0"))
    total_transactions: int = 0
    average_transaction_value: Decimal = Field(default=Decimal("0"))


class LeadSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_leads: int = 0
    new_leads: int = 0
    contacted_leads: int = 0
    qualified_leads: int = 0
    negotiation_leads: int = 0
    converted_leads: int = 0
    lost_leads: int = 0
    conversion_rate: float = 0.0


class BookingSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_bookings: int = 0
    pending_bookings: int = 0
    confirmed_bookings: int = 0
    cancelled_bookings: int = 0
    completed_bookings: int = 0
    total_booking_value: Decimal = Field(default=Decimal("0"))


class PropertySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_properties: int = 0
    available_properties: int = 0
    reserved_properties: int = 0
    sold_properties: int = 0
    rented_properties: int = 0
    inactive_properties: int = 0
    average_property_price: Decimal = Field(default=Decimal("0"))


class CustomerSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_customers: int = 0
    new_customers_this_month: int = 0
    active_customers: int = 0
    distinct_cities: int = 0


class AgentPerformance(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_id: int
    agent_name: str
    total_leads_assigned: int = 0
    total_leads_converted: int = 0
    conversion_rate: float = 0.0
    total_bookings: int = 0
    total_revenue_generated: Decimal = Field(default=Decimal("0"))
    average_deal_size: Decimal = Field(default=Decimal("0"))


class RecentActivity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    activity_type: ActivityType
    # FIX: was `int` -- Customer.id/Lead.id/Booking.id/Payment.id (every
    # real source of this field) are all UUID primary keys in this
    # schema, not integers. An `int`-typed field would fail Pydantic
    # validation on literally every populated activity.
    reference_id: Union[UUID, int]
    title: str
    description: Optional[str] = None
    actor_name: Optional[str] = None
    amount: Optional[Decimal] = None
    occurred_at: datetime


class MonthlyTrend(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    year: int
    month: int
    label: str
    total_count: int = 0
    total_amount: Decimal = Field(default=Decimal("0"))


class WeeklyTrend(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    year: int
    week_number: int
    week_start: date
    week_end: date
    total_count: int = 0
    total_amount: Decimal = Field(default=Decimal("0"))


class RevenueChart(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period: TrendPeriod
    labels: List[str] = Field(default_factory=list)
    values: List[Decimal] = Field(default_factory=list)


class BookingChart(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period: TrendPeriod
    labels: List[str] = Field(default_factory=list)
    values: List[int] = Field(default_factory=list)


class LeadChart(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period: TrendPeriod
    labels: List[str] = Field(default_factory=list)
    new_leads: List[int] = Field(default_factory=list)
    converted_leads: List[int] = Field(default_factory=list)


class PropertyChart(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    labels: List[str] = Field(default_factory=list)
    values: List[int] = Field(default_factory=list)


class PaymentChart(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period: TrendPeriod
    labels: List[str] = Field(default_factory=list)
    collected: List[Decimal] = Field(default_factory=list)
    pending: List[Decimal] = Field(default_factory=list)


class DashboardSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    revenue: RevenueSummary
    leads: LeadSummary
    bookings: BookingSummary
    properties: PropertySummary
    customers: CustomerSummary
    generated_at: datetime


class DashboardResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    summary: DashboardSummary
    top_agents: List[AgentPerformance] = Field(default_factory=list)
    recent_activities: List[RecentActivity] = Field(default_factory=list)
    revenue_chart: Optional[RevenueChart] = None
    booking_chart: Optional[BookingChart] = None
    lead_chart: Optional[LeadChart] = None
    property_chart: Optional[PropertyChart] = None
    payment_chart: Optional[PaymentChart] = None