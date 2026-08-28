from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestException, ValidationException
from app.models.dashboard import ActivityType, TrendPeriod
from app.models.lead import LeadStatus
from app.models.payment import PaymentStatus
from app.models.user import User, UserRole
from app.repositories.dashboard_repository import DashboardRepository
from app.schemas.dashboard import (
    AgentPerformance,
    BookingChart,
    BookingSummary,
    CustomerSummary,
    DashboardResponse,
    DashboardSummary,
    LeadChart,
    LeadSummary,
    MonthlyTrend,
    PaymentChart,
    PropertyChart,
    PropertySummary,
    RecentActivity,
    RevenueChart,
    RevenueSummary,
    WeeklyTrend,
)


class DashboardService:
    """
    Business logic layer for the Dashboard module.
    Strictly read-only: never inserts, updates, or deletes any record.
    Orchestrates the DashboardRepository and applies role-based visibility rules.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.repository = DashboardRepository(db)

    # ------------------------------------------------------------------
    # Access control helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_role(current_user: User) -> Optional[str]:
        role = getattr(current_user, "role", None)
        return role.value if hasattr(role, "value") else role

    def _validate_access(self, current_user: Optional[User]) -> None:
        if current_user is None:
            raise ValidationException("Authenticated user is required to access the dashboard.")
        allowed_roles = {
            UserRole.ADMIN.value,
            UserRole.SALES_MANAGER.value,
            UserRole.SALES_AGENT.value,
        }
        role_value = self._extract_role(current_user)
        if role_value not in allowed_roles:
            raise ValidationException("You are not authorized to access the dashboard module.")

    def _is_agent_scope(self, current_user: User) -> bool:
        return self._extract_role(current_user) == UserRole.SALES_AGENT.value

    # ------------------------------------------------------------------
    # Calculation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_growth(current: Optional[Decimal], previous: Optional[Decimal]) -> float:
        current_value = current or Decimal("0")
        previous_value = previous or Decimal("0")
        if previous_value == 0:
            return 100.0 if current_value > 0 else 0.0
        return round(float((current_value - previous_value) / previous_value * 100), 2)

    @staticmethod
    def _calculate_booking_conversion_rate(stats: Dict[str, Any]) -> float:
        total = stats.get("total_bookings", 0) or 0
        completed = stats.get("completed_bookings", 0) or 0
        confirmed = stats.get("confirmed_bookings", 0) or 0
        if total == 0:
            return 0.0
        return round(((completed + confirmed) / total) * 100, 2)

    @staticmethod
    def _calculate_property_availability_rate(stats: Dict[str, Any]) -> float:
        total = stats.get("total_properties", 0) or 0
        available = stats.get("available_properties", 0) or 0
        if total == 0:
            return 0.0
        return round((available / total) * 100, 2)

    def _map_agent_performance(self, row: Any) -> AgentPerformance:
        total_leads_assigned = row.total_leads_assigned or 0
        total_leads_converted = row.total_leads_converted or 0
        total_bookings = row.total_bookings or 0
        total_revenue_generated = row.total_revenue_generated or Decimal("0")
        conversion_rate = (
            round((total_leads_converted / total_leads_assigned) * 100, 2)
            if total_leads_assigned > 0
            else 0.0
        )
        average_deal_size = (
            round(total_revenue_generated / total_bookings, 2)
            if total_bookings > 0
            else Decimal("0")
        )
        return AgentPerformance(
            agent_id=row.agent_id,
            agent_name=row.agent_name,
            total_leads_assigned=total_leads_assigned,
            total_leads_converted=total_leads_converted,
            conversion_rate=conversion_rate,
            total_bookings=total_bookings,
            total_revenue_generated=total_revenue_generated,
            average_deal_size=average_deal_size,
        )

    def _map_top_agent(self, row: Any) -> AgentPerformance:
        total_bookings = row.total_bookings or 0
        total_revenue_generated = row.total_revenue_generated or Decimal("0")
        average_deal_size = (
            round(total_revenue_generated / total_bookings, 2)
            if total_bookings > 0
            else Decimal("0")
        )
        return AgentPerformance(
            agent_id=row.agent_id,
            agent_name=row.agent_name,
            total_leads_assigned=0,
            total_leads_converted=0,
            conversion_rate=0.0,
            total_bookings=total_bookings,
            total_revenue_generated=total_revenue_generated,
            average_deal_size=average_deal_size,
        )

    # ------------------------------------------------------------------
    # Summary sections
    # ------------------------------------------------------------------

    async def get_revenue_overview(self, current_user: User) -> RevenueSummary:
        self._validate_access(current_user)
        stats = await self.repository.get_payment_statistics()
        return RevenueSummary(**stats)

    async def get_payment_overview(self, current_user: User) -> RevenueSummary:
        self._validate_access(current_user)
        stats = await self.repository.get_payment_statistics()
        return RevenueSummary(**stats)

    async def get_lead_overview(self, current_user: User) -> LeadSummary:
        self._validate_access(current_user)
        stats = await self.repository.get_lead_statistics()
        conversion_rate = await self.repository.get_lead_conversion_rate()
        return LeadSummary(**stats, conversion_rate=conversion_rate)

    async def get_booking_overview(self, current_user: User) -> BookingSummary:
        self._validate_access(current_user)
        stats = await self.repository.get_booking_statistics()
        return BookingSummary(**stats)

    async def get_property_overview(self, current_user: User) -> PropertySummary:
        self._validate_access(current_user)
        stats = await self.repository.get_property_statistics()
        return PropertySummary(**stats)

    async def get_customer_overview(self, current_user: User) -> CustomerSummary:
        self._validate_access(current_user)
        stats = await self.repository.get_customer_statistics()
        return CustomerSummary(**stats)

    async def get_dashboard_summary(self, current_user: User) -> DashboardSummary:
        self._validate_access(current_user)
        revenue = await self.get_revenue_overview(current_user)
        leads = await self.get_lead_overview(current_user)
        bookings = await self.get_booking_overview(current_user)
        properties = await self.get_property_overview(current_user)
        customers = await self.get_customer_overview(current_user)
        return DashboardSummary(
            revenue=revenue,
            leads=leads,
            bookings=bookings,
            properties=properties,
            customers=customers,
            generated_at=datetime.utcnow(),
        )

    # ------------------------------------------------------------------
    # Revenue trends and growth
    # ------------------------------------------------------------------

    async def get_monthly_revenue_trend(
        self, current_user: User, months: int = 12
    ) -> List[MonthlyTrend]:
        self._validate_access(current_user)
        if months <= 0 or months > 36:
            raise BadRequestException("months must be between 1 and 36.")
        rows = await self.repository.get_revenue_trend(months=months)
        trends: List[MonthlyTrend] = []
        for row in rows:
            year = int(row.year)
            month = int(row.month)
            label = date(year, month, 1).strftime("%b %Y")
            trends.append(
                MonthlyTrend(
                    year=year,
                    month=month,
                    label=label,
                    total_count=row.total_count or 0,
                    total_amount=row.total_amount or Decimal("0"),
                )
            )
        trends.reverse()
        return trends

    async def get_weekly_revenue_trend(
        self, current_user: User, weeks: int = 8
    ) -> List[WeeklyTrend]:
        self._validate_access(current_user)
        if weeks <= 0 or weeks > 52:
            raise BadRequestException("weeks must be between 1 and 52.")

        trends: List[WeeklyTrend] = []
        today = date.today()
        current_week_start = today - timedelta(days=today.weekday())

        for index in range(weeks):
            week_start = current_week_start - timedelta(weeks=index)
            week_end = week_start + timedelta(days=6)
            total_amount = await self.repository.get_weekly_revenue(week_start, week_end)
            iso_year, iso_week, _ = week_start.isocalendar()
            trends.append(
                WeeklyTrend(
                    year=iso_year,
                    week_number=iso_week,
                    week_start=week_start,
                    week_end=week_end,
                    total_count=0,
                    total_amount=total_amount,
                )
            )

        trends.reverse()
        return trends

    async def get_daily_revenue_overview(self, current_user: User) -> Dict[str, Any]:
        self._validate_access(current_user)

        today = date.today()
        yesterday = today - timedelta(days=1)
        week_start = today - timedelta(days=today.weekday())
        previous_week_start = week_start - timedelta(days=7)
        previous_week_end = week_start - timedelta(days=1)

        if today.month == 1:
            previous_month_year, previous_month = today.year - 1, 12
        else:
            previous_month_year, previous_month = today.year, today.month - 1

        today_revenue = await self.repository.get_weekly_revenue(today, today)
        yesterday_revenue = await self.repository.get_weekly_revenue(yesterday, yesterday)
        week_to_date_revenue = await self.repository.get_weekly_revenue(week_start, today)
        previous_week_revenue = await self.repository.get_weekly_revenue(
            previous_week_start, previous_week_end
        )
        month_to_date_revenue = await self.repository.get_monthly_revenue(
            today.year, today.month
        )
        previous_month_revenue = await self.repository.get_monthly_revenue(
            previous_month_year, previous_month
        )

        booking_stats = await self.repository.get_booking_statistics()
        property_stats = await self.repository.get_property_statistics()
        outstanding_amount = await self.repository.get_outstanding_revenue()

        return {
            "date": today,
            "today_revenue": today_revenue,
            "yesterday_revenue": yesterday_revenue,
            "daily_growth_percentage": self._calculate_growth(today_revenue, yesterday_revenue),
            "week_to_date_revenue": week_to_date_revenue,
            "previous_week_revenue": previous_week_revenue,
            "weekly_growth_percentage": self._calculate_growth(
                week_to_date_revenue, previous_week_revenue
            ),
            "month_to_date_revenue": month_to_date_revenue,
            "previous_month_revenue": previous_month_revenue,
            "monthly_growth_percentage": self._calculate_growth(
                month_to_date_revenue, previous_month_revenue
            ),
            "booking_conversion_rate": self._calculate_booking_conversion_rate(booking_stats),
            "property_availability_rate": self._calculate_property_availability_rate(
                property_stats
            ),
            "outstanding_amount": outstanding_amount,
        }

    # ------------------------------------------------------------------
    # Agent performance
    # ------------------------------------------------------------------

    async def get_agents_performance(
        self, current_user: User, limit: int = 20, offset: int = 0
    ) -> List[AgentPerformance]:
        self._validate_access(current_user)
        if limit <= 0 or offset < 0:
            raise BadRequestException("Invalid pagination parameters.")
        rows = await self.repository.get_agent_performance(limit=limit, offset=offset)
        performances = [self._map_agent_performance(row) for row in rows]
        if self._is_agent_scope(current_user):
            performances = [p for p in performances if p.agent_id == current_user.id]
        return performances

    async def get_top_agents(self, current_user: User, limit: int = 5) -> List[AgentPerformance]:
        self._validate_access(current_user)
        if limit <= 0:
            raise BadRequestException("limit must be greater than zero.")
        rows = await self.repository.get_top_agents(limit=limit)
        performances = [self._map_top_agent(row) for row in rows]
        if self._is_agent_scope(current_user):
            performances = [p for p in performances if p.agent_id == current_user.id]
        return performances

    # ------------------------------------------------------------------
    # Rankings
    # ------------------------------------------------------------------

    async def get_top_properties(
        self, current_user: User, limit: int = 5
    ) -> List[Dict[str, Any]]:
        self._validate_access(current_user)
        if limit <= 0:
            raise BadRequestException("limit must be greater than zero.")
        rows = await self.repository.get_top_properties(limit=limit)
        return [
            {
                "property_id": row.property_id,
                "title": row.title,
                "city": row.city,
                "total_bookings": row.total_bookings or 0,
                "total_revenue": row.total_revenue or Decimal("0"),
            }
            for row in rows
        ]

    async def get_top_cities(self, current_user: User, limit: int = 5) -> List[Dict[str, Any]]:
        self._validate_access(current_user)
        if limit <= 0:
            raise BadRequestException("limit must be greater than zero.")
        rows = await self.repository.get_top_cities(limit=limit)
        return [
            {
                "city": row.city,
                "total_properties": row.total_properties or 0,
                "total_bookings": row.total_bookings or 0,
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Follow-ups
    # ------------------------------------------------------------------

    async def get_upcoming_followups(
        self, current_user: User, limit: int = 10, offset: int = 0
    ) -> List[Dict[str, Any]]:
        self._validate_access(current_user)
        if limit <= 0 or offset < 0:
            raise BadRequestException("Invalid pagination parameters.")
        leads = await self.repository.get_upcoming_followups(limit=limit, offset=offset)
        if self._is_agent_scope(current_user):
            # FIX: was `getattr(lead, "agent_id", None)` -- Lead has no
            # "agent_id" field (real field is "assigned_agent_id"), so
            # this always evaluated to None and silently hid every
            # follow-up from every SALES_AGENT user.
            leads = [
                lead
                for lead in leads
                if getattr(lead, "assigned_agent_id", None) == current_user.id
            ]
        return [
            {
                "lead_id": lead.id,
                # FIX: Lead has no "customer_id" -- it is a standalone
                # prospect record with its own contact fields, not an FK
                # to Customer. Exposing the lead's own name is the closest
                # real equivalent to what this key was meant to convey.
                "lead_name": lead.full_name,
                "status": lead.status,
                # FIX: real field is "next_follow_up", not "follow_up_date".
                "follow_up_date": lead.next_follow_up,
                "agent_id": getattr(lead, "assigned_agent_id", None),
            }
            for lead in leads
        ]

    # ------------------------------------------------------------------
    # Recent activities
    # ------------------------------------------------------------------

    async def get_recent_activities(
        self, current_user: User, limit: int = 15
    ) -> List[RecentActivity]:
        self._validate_access(current_user)
        if limit <= 0:
            raise BadRequestException("limit must be greater than zero.")

        customers = await self.repository.get_recent_customers(limit=limit)
        leads = await self.repository.get_recent_leads(limit=limit)
        bookings = await self.repository.get_recent_bookings(limit=limit)
        payments = await self.repository.get_recent_payments(limit=limit)

        activities: List[RecentActivity] = []

        for customer in customers:
            activities.append(
                RecentActivity(
                    activity_type=ActivityType.CUSTOMER_CREATED,
                    reference_id=customer.id,
                    title=f"New customer registered: {customer.full_name}",
                    description=getattr(customer, "email", None),
                    actor_name=getattr(customer, "full_name", None),
                    amount=None,
                    occurred_at=customer.created_at,
                )
            )

        for lead in leads:
            # FIX: was `LeadStatusEnum.CONVERTED.value` (lowercase
            # "converted", a member that does not even exist on the real
            # LeadStatus enum) -- always False, so leads were never
            # reported as converted. Real terminal "converted" state is
            # LeadStatus.BOOKED.
            is_converted = str(getattr(lead, "status", "")) == LeadStatus.BOOKED.value
            activities.append(
                RecentActivity(
                    activity_type=(
                        ActivityType.LEAD_CONVERTED if is_converted else ActivityType.LEAD_CREATED
                    ),
                    reference_id=lead.id,
                    title="Lead converted" if is_converted else "New lead captured",
                    description=f"Lead status: {lead.status}",
                    actor_name=None,
                    amount=None,
                    occurred_at=getattr(lead, "updated_at", None) or lead.created_at,
                )
            )

        for booking in bookings:
            activities.append(
                RecentActivity(
                    activity_type=ActivityType.BOOKING_CREATED,
                    reference_id=booking.id,
                    title="New booking created",
                    description=f"Booking status: {booking.status}",
                    actor_name=None,
                    # FIX: Booking has no "amount" field -- real field is
                    # "booking_amount". Direct attribute access here raised
                    # AttributeError for every request that returned at
                    # least one recent booking.
                    amount=getattr(booking, "booking_amount", None),
                    occurred_at=booking.created_at,
                )
            )

        for payment in payments:
            # FIX: Payment has no "status" field (real field is
            # "payment_status"), and PaymentStatusEnum.COMPLETED
            # ("completed") does not correspond to any real PaymentStatus
            # member -- real "paid" status is PaymentStatus.SUCCESS.
            is_completed = payment.payment_status == PaymentStatus.SUCCESS.value
            activities.append(
                RecentActivity(
                    activity_type=(
                        ActivityType.PAYMENT_RECEIVED
                        if is_completed
                        else ActivityType.PAYMENT_PENDING
                    ),
                    reference_id=payment.id,
                    title="Payment received" if is_completed else "Payment pending",
                    description=f"Payment status: {payment.payment_status}",
                    actor_name=None,
                    # FIX: Payment has no "amount" field -- real field is
                    # "payment_amount". Direct attribute access here raised
                    # AttributeError for every request that returned at
                    # least one recent payment.
                    amount=getattr(payment, "payment_amount", None),
                    occurred_at=payment.created_at,
                )
            )

        activities.sort(key=lambda item: item.occurred_at, reverse=True)
        return activities[:limit]

    # ------------------------------------------------------------------
    # Charts
    # ------------------------------------------------------------------

    async def _build_revenue_chart(self) -> RevenueChart:
        rows = list(reversed(await self.repository.get_revenue_trend(months=6)))
        labels = [date(int(row.year), int(row.month), 1).strftime("%b %Y") for row in rows]
        values = [row.total_amount or Decimal("0") for row in rows]
        return RevenueChart(period=TrendPeriod.MONTHLY, labels=labels, values=values)

    async def _build_booking_chart(self) -> BookingChart:
        stats = await self.repository.get_booking_statistics()
        labels = ["Pending", "Confirmed", "Cancelled", "Completed"]
        values = [
            stats.get("pending_bookings", 0),
            stats.get("confirmed_bookings", 0),
            stats.get("cancelled_bookings", 0),
            stats.get("completed_bookings", 0),
        ]
        return BookingChart(period=TrendPeriod.MONTHLY, labels=labels, values=values)

    async def _build_lead_chart(self) -> LeadChart:
        stats = await self.repository.get_lead_statistics()
        labels = ["New", "Contacted", "Qualified", "Negotiation", "Converted", "Lost"]
        new_leads = [
            stats.get("new_leads", 0),
            stats.get("contacted_leads", 0),
            stats.get("qualified_leads", 0),
            stats.get("negotiation_leads", 0),
            stats.get("converted_leads", 0),
            stats.get("lost_leads", 0),
        ]
        converted_leads = [0, 0, 0, 0, stats.get("converted_leads", 0), 0]
        return LeadChart(
            period=TrendPeriod.MONTHLY,
            labels=labels,
            new_leads=new_leads,
            converted_leads=converted_leads,
        )

    async def _build_property_chart(self) -> PropertyChart:
        stats = await self.repository.get_property_statistics()
        labels = ["Available", "Reserved", "Sold", "Rented", "Inactive"]
        values = [
            stats.get("available_properties", 0),
            stats.get("reserved_properties", 0),
            stats.get("sold_properties", 0),
            stats.get("rented_properties", 0),
            stats.get("inactive_properties", 0),
        ]
        return PropertyChart(labels=labels, values=values)

    async def _build_payment_chart(self) -> PaymentChart:
        stats = await self.repository.get_payment_statistics()
        labels = ["Total"]
        collected = [stats.get("collected_revenue", Decimal("0"))]
        pending = [stats.get("pending_revenue", Decimal("0"))]
        return PaymentChart(
            period=TrendPeriod.MONTHLY, labels=labels, collected=collected, pending=pending
        )

    # ------------------------------------------------------------------
    # Full dashboard
    # ------------------------------------------------------------------

    async def get_full_dashboard(self, current_user: User) -> DashboardResponse:
        self._validate_access(current_user)

        summary = await self.get_dashboard_summary(current_user)
        top_agents = await self.get_top_agents(current_user, limit=5)
        recent_activities = await self.get_recent_activities(current_user, limit=15)

        revenue_chart = await self._build_revenue_chart()
        booking_chart = await self._build_booking_chart()
        lead_chart = await self._build_lead_chart()
        property_chart = await self._build_property_chart()
        payment_chart = await self._build_payment_chart()

        return DashboardResponse(
            summary=summary,
            top_agents=top_agents,
            recent_activities=recent_activities,
            revenue_chart=revenue_chart,
            booking_chart=booking_chart,
            lead_chart=lead_chart,
            property_chart=property_chart,
            payment_chart=payment_chart,
        )