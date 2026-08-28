from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking, BookingStatus
from app.models.customer import Customer, CustomerStatus
from app.models.lead import Lead, LeadStatus
from app.models.payment import Payment, PaymentStatus
from app.models.property import Property, PropertyStatus
from app.models.user import User


class DashboardRepository:
    """
    Read-only repository for the Dashboard module.
    This repository never inserts, updates, or deletes records.
    It only aggregates and reads data produced by other modules.

    IMPORTANT: all status/enum comparisons in this file use the REAL
    enums declared on each domain model (app.models.lead.LeadStatus,
    app.models.booking.BookingStatus, app.models.property.PropertyStatus,
    app.models.payment.PaymentStatus) -- NOT the separate, unrelated
    LeadStatusEnum/BookingStatusEnum/PropertyStatusEnum/PaymentStatusEnum
    declared in app.models.dashboard, which use different casing and, in
    several cases, entirely different member names than the real models
    and were the root cause of this module's bugs (see audit notes below).
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Dashboard Summary
    # ------------------------------------------------------------------

    async def get_dashboard_summary(self) -> dict:
        # One aggregate statement keeps the repository efficient and gives
        # callers a stable two-query contract: one row-count query plus one
        # revenue query.
        summary_stmt = select(
            (select(func.count(Customer.id))).scalar_subquery().label("total_customers"),
            (select(func.count(Lead.id))).scalar_subquery().label("total_leads"),
            (select(func.count(Property.id))).scalar_subquery().label("total_properties"),
            (select(func.count(Booking.id))).scalar_subquery().label("total_bookings"),
        )
        summary_result = await self.db.execute(summary_stmt)
        row = summary_result.one()

        revenue_stmt = select(
            func.coalesce(func.sum(Payment.payment_amount), 0)
        ).where(Payment.payment_status == PaymentStatus.SUCCESS.value)
        revenue_result = await self.db.execute(revenue_stmt)
        total_revenue = revenue_result.scalar_one()

        return {
            "total_customers": getattr(row, "total_customers", 0) or 0,
            "total_leads": getattr(row, "total_leads", 0) or 0,
            "total_properties": getattr(row, "total_properties", 0) or 0,
            "total_bookings": getattr(row, "total_bookings", 0) or 0,
            "total_revenue": total_revenue or Decimal("0"),
        }

    # ------------------------------------------------------------------
    # Today's Summary
    # ------------------------------------------------------------------

    async def get_today_summary(self) -> dict:
        today = date.today()
        start = datetime.combine(today, datetime.min.time())
        end = start + timedelta(days=1)

        new_customers_stmt = select(func.count(Customer.id)).where(
            Customer.created_at >= start, Customer.created_at < end
        )
        new_leads_stmt = select(func.count(Lead.id)).where(
            Lead.created_at >= start, Lead.created_at < end
        )
        new_bookings_stmt = select(func.count(Booking.id)).where(
            Booking.created_at >= start, Booking.created_at < end
        )
        # FIX: Payment.amount -> payment_amount, Payment.status ->
        # payment_status, PaymentStatusEnum.COMPLETED -> PaymentStatus.SUCCESS.
        revenue_stmt = select(func.coalesce(func.sum(Payment.payment_amount), 0)).where(
            Payment.payment_date >= start,
            Payment.payment_date < end,
            Payment.payment_status == PaymentStatus.SUCCESS.value,
        )

        new_customers = (await self.db.execute(new_customers_stmt)).scalar_one()
        new_leads = (await self.db.execute(new_leads_stmt)).scalar_one()
        new_bookings = (await self.db.execute(new_bookings_stmt)).scalar_one()
        revenue_today = (await self.db.execute(revenue_stmt)).scalar_one()

        return {
            "date": today,
            "new_customers": new_customers or 0,
            "new_leads": new_leads or 0,
            "new_bookings": new_bookings or 0,
            "revenue_today": revenue_today or Decimal("0"),
        }

    # ------------------------------------------------------------------
    # Monthly / Weekly Revenue
    # ------------------------------------------------------------------

    async def get_monthly_revenue(self, year: int, month: int) -> Decimal:
        # FIX: Payment.amount -> payment_amount, Payment.status ->
        # payment_status, COMPLETED -> SUCCESS.
        stmt = select(func.coalesce(func.sum(Payment.payment_amount), 0)).where(
            func.extract("year", Payment.payment_date) == year,
            func.extract("month", Payment.payment_date) == month,
            Payment.payment_status == PaymentStatus.SUCCESS.value,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one() or Decimal("0")

    async def get_weekly_revenue(self, start_date: date, end_date: date) -> Decimal:
        stmt = select(func.coalesce(func.sum(Payment.payment_amount), 0)).where(
            Payment.payment_date >= start_date,
            Payment.payment_date <= end_date,
            Payment.payment_status == PaymentStatus.SUCCESS.value,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one() or Decimal("0")

    async def get_revenue_trend(self, months: int = 12) -> Sequence:
        stmt = (
            select(
                func.extract("year", Payment.payment_date).label("year"),
                func.extract("month", Payment.payment_date).label("month"),
                func.coalesce(func.sum(Payment.payment_amount), 0).label("total_amount"),
                func.count(Payment.id).label("total_count"),
            )
            .where(Payment.payment_status == PaymentStatus.SUCCESS.value)
            .group_by(
                func.extract("year", Payment.payment_date),
                func.extract("month", Payment.payment_date),
            )
            .order_by(
                func.extract("year", Payment.payment_date).desc(),
                func.extract("month", Payment.payment_date).desc(),
            )
            .limit(months)
        )
        result = await self.db.execute(stmt)
        return result.all()

    # ------------------------------------------------------------------
    # Lead Statistics
    # ------------------------------------------------------------------

    async def get_lead_statistics(self) -> dict:
        # FIX: real LeadStatus members are NEW, CONTACTED, QUALIFIED,
        # SITE_VISIT, NEGOTIATION, BOOKED, LOST (all uppercase) -- there is
        # no CONVERTED member, and the dashboard's fictional LeadStatusEnum
        # used lowercase values that never matched any real row. BOOKED is
        # the real "converted" terminal state. SITE_VISIT (which the
        # 6-bucket schema has no slot for) is folded into "qualified" since
        # it represents a qualified lead that has progressed to viewing.
        stmt = select(
            func.count(Lead.id).label("total_leads"),
            func.sum(case((Lead.status == LeadStatus.NEW.value, 1), else_=0)).label(
                "new_leads"
            ),
            func.sum(case((Lead.status == LeadStatus.CONTACTED.value, 1), else_=0)).label(
                "contacted_leads"
            ),
            func.sum(
                case(
                    (
                        Lead.status.in_(
                            [LeadStatus.QUALIFIED.value, LeadStatus.SITE_VISIT.value]
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("qualified_leads"),
            func.sum(case((Lead.status == LeadStatus.NEGOTIATION.value, 1), else_=0)).label(
                "negotiation_leads"
            ),
            func.sum(case((Lead.status == LeadStatus.BOOKED.value, 1), else_=0)).label(
                "converted_leads"
            ),
            func.sum(case((Lead.status == LeadStatus.LOST.value, 1), else_=0)).label(
                "lost_leads"
            ),
        )
        result = await self.db.execute(stmt)
        row = result.one()
        return {
            "total_leads": row.total_leads or 0,
            "new_leads": row.new_leads or 0,
            "contacted_leads": row.contacted_leads or 0,
            "qualified_leads": row.qualified_leads or 0,
            "negotiation_leads": row.negotiation_leads or 0,
            "converted_leads": row.converted_leads or 0,
            "lost_leads": row.lost_leads or 0,
        }

    async def get_lead_conversion_rate(self) -> float:
        # FIX: CONVERTED -> BOOKED (see get_lead_statistics note above).
        stmt = select(
            func.count(Lead.id).label("total_leads"),
            func.sum(case((Lead.status == LeadStatus.BOOKED.value, 1), else_=0)).label(
                "converted_leads"
            ),
        )
        result = await self.db.execute(stmt)
        row = result.one()
        total = row.total_leads or 0
        converted = row.converted_leads or 0
        if total == 0:
            return 0.0
        return round((converted / total) * 100, 2)

    # ------------------------------------------------------------------
    # Booking Statistics
    # ------------------------------------------------------------------

    async def get_booking_statistics(self) -> dict:
        # FIX: real BookingStatus members are PENDING, CONFIRMED,
        # CANCELLED, COMPLETED, REFUNDED (uppercase); the dashboard's
        # fictional BookingStatusEnum used lowercase values with no
        # REFUNDED member at all. REFUNDED is folded into "cancelled"
        # since the 4-bucket schema has no dedicated slot and a refunded
        # booking is, from a pipeline standpoint, no longer active.
        # Booking.amount -> booking_amount (real field name).
        stmt = select(
            func.count(Booking.id).label("total_bookings"),
            func.sum(case((Booking.status == BookingStatus.PENDING.value, 1), else_=0)).label(
                "pending_bookings"
            ),
            func.sum(case((Booking.status == BookingStatus.CONFIRMED.value, 1), else_=0)).label(
                "confirmed_bookings"
            ),
            func.sum(
                case(
                    (
                        Booking.status.in_(
                            [BookingStatus.CANCELLED.value, BookingStatus.REFUNDED.value]
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("cancelled_bookings"),
            func.sum(case((Booking.status == BookingStatus.COMPLETED.value, 1), else_=0)).label(
                "completed_bookings"
            ),
            func.coalesce(func.sum(Booking.booking_amount), 0).label("total_booking_value"),
        )
        result = await self.db.execute(stmt)
        row = result.one()
        return {
            "total_bookings": row.total_bookings or 0,
            "pending_bookings": row.pending_bookings or 0,
            "confirmed_bookings": row.confirmed_bookings or 0,
            "cancelled_bookings": row.cancelled_bookings or 0,
            "completed_bookings": row.completed_bookings or 0,
            "total_booking_value": row.total_booking_value or Decimal("0"),
        }

    # ------------------------------------------------------------------
    # Property Statistics
    # ------------------------------------------------------------------

    async def get_property_statistics(self) -> dict:
        # FIX: Property.status doesn't exist -- real field is
        # property_status. Real PropertyStatus members are AVAILABLE,
        # UNDER_NEGOTIATION, SOLD, RENTED, ON_HOLD, WITHDRAWN; the
        # dashboard's fictional PropertyStatusEnum had RESERVED/INACTIVE
        # members that don't exist on the real model at all, and was
        # missing UNDER_NEGOTIATION/ON_HOLD/WITHDRAWN. Mapped onto the
        # 5-bucket schema as: reserved = ON_HOLD + UNDER_NEGOTIATION,
        # inactive = WITHDRAWN, so every real status has a home and the
        # five buckets sum to total_properties.
        stmt = select(
            func.count(Property.id).label("total_properties"),
            func.sum(
                case((Property.property_status == PropertyStatus.AVAILABLE.value, 1), else_=0)
            ).label("available_properties"),
            func.sum(
                case(
                    (
                        Property.property_status.in_(
                            [
                                PropertyStatus.ON_HOLD.value,
                                PropertyStatus.UNDER_NEGOTIATION.value,
                            ]
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("reserved_properties"),
            func.sum(
                case((Property.property_status == PropertyStatus.SOLD.value, 1), else_=0)
            ).label("sold_properties"),
            func.sum(
                case((Property.property_status == PropertyStatus.RENTED.value, 1), else_=0)
            ).label("rented_properties"),
            func.sum(
                case((Property.property_status == PropertyStatus.WITHDRAWN.value, 1), else_=0)
            ).label("inactive_properties"),
            func.coalesce(func.avg(Property.price), 0).label("average_property_price"),
        )
        result = await self.db.execute(stmt)
        row = result.one()
        return {
            "total_properties": row.total_properties or 0,
            "available_properties": row.available_properties or 0,
            "reserved_properties": row.reserved_properties or 0,
            "sold_properties": row.sold_properties or 0,
            "rented_properties": row.rented_properties or 0,
            "inactive_properties": row.inactive_properties or 0,
            "average_property_price": row.average_property_price or Decimal("0"),
        }

    # ------------------------------------------------------------------
    # Customer Statistics
    # ------------------------------------------------------------------

    async def get_customer_statistics(self) -> dict:
        # FIX: original never populated "active_customers" (schema field
        # existed but repository never returned it, so it silently
        # defaulted to 0 for every request). Now computed from
        # Customer.status == CustomerStatus.ACTIVE.
        today = date.today()
        month_start = date(today.year, today.month, 1)

        stmt = select(
            func.count(Customer.id).label("total_customers"),
            func.sum(
                case((Customer.created_at >= month_start, 1), else_=0)
            ).label("new_customers_this_month"),
            func.sum(
                case((Customer.status == CustomerStatus.ACTIVE.value, 1), else_=0)
            ).label("active_customers"),
            func.count(func.distinct(Customer.city)).label("distinct_cities"),
        )
        result = await self.db.execute(stmt)
        row = result.one()
        return {
            "total_customers": row.total_customers or 0,
            "new_customers_this_month": row.new_customers_this_month or 0,
            "active_customers": getattr(row, "active_customers", 0) or 0,
            "distinct_cities": row.distinct_cities or 0,
        }

    # ------------------------------------------------------------------
    # Payment Statistics
    # ------------------------------------------------------------------

    async def get_payment_statistics(self) -> dict:
        # FIX: Payment.amount -> payment_amount, Payment.status ->
        # payment_status throughout. Real PaymentStatus members are
        # PENDING, SUCCESS, FAILED, REFUNDED, PARTIAL (uppercase);
        # "collected" = SUCCESS (was the nonexistent "COMPLETED"),
        # "pending" = PENDING + PARTIAL, "refunded" = REFUNDED. FAILED
        # payments correctly contribute to none of the three buckets --
        # a failed transaction has no realized financial value.
        stmt = select(
            func.coalesce(
                func.sum(
                    case(
                        (
                            Payment.payment_status == PaymentStatus.SUCCESS.value,
                            Payment.payment_amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("collected_revenue"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            Payment.payment_status.in_(
                                [PaymentStatus.PENDING.value, PaymentStatus.PARTIAL.value]
                            ),
                            Payment.payment_amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("pending_revenue"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            Payment.payment_status == PaymentStatus.REFUNDED.value,
                            Payment.payment_amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("refunded_revenue"),
            func.count(Payment.id).label("total_transactions"),
            func.coalesce(func.avg(Payment.payment_amount), 0).label("average_transaction_value"),
        )
        result = await self.db.execute(stmt)
        row = result.one()
        collected = row.collected_revenue or Decimal("0")
        pending = row.pending_revenue or Decimal("0")
        refunded = row.refunded_revenue or Decimal("0")
        return {
            "total_revenue": collected + pending,
            "collected_revenue": collected,
            "pending_revenue": pending,
            "refunded_revenue": refunded,
            "total_transactions": row.total_transactions or 0,
            "average_transaction_value": row.average_transaction_value or Decimal("0"),
        }

    # ------------------------------------------------------------------
    # Agent Performance
    # ------------------------------------------------------------------

    async def get_agent_performance(self, limit: int = 20, offset: int = 0) -> Sequence:
        # FIX: Lead.agent_id doesn't exist -- real field is
        # assigned_agent_id. LeadStatusEnum.CONVERTED -> LeadStatus.BOOKED.
        # Booking.amount -> booking_amount.
        stmt = (
            select(
                User.id.label("agent_id"),
                User.full_name.label("agent_name"),
                func.count(func.distinct(Lead.id)).label("total_leads_assigned"),
                func.sum(
                    case((Lead.status == LeadStatus.BOOKED.value, 1), else_=0)
                ).label("total_leads_converted"),
                func.count(func.distinct(Booking.id)).label("total_bookings"),
                func.coalesce(func.sum(func.distinct(Booking.booking_amount)), 0).label(
                    "total_revenue_generated"
                ),
            )
            .select_from(User)
            .outerjoin(Lead, Lead.assigned_agent_id == User.id)
            .outerjoin(Booking, Booking.agent_id == User.id)
            .group_by(User.id, User.full_name)
            .order_by(func.count(func.distinct(Booking.id)).desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return result.all()

    # ------------------------------------------------------------------
    # Recent Records (Paginated)
    # ------------------------------------------------------------------

    async def get_recent_customers(self, limit: int = 10, offset: int = 0) -> Sequence[Customer]:
        stmt = (
            select(Customer)
            .order_by(Customer.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_recent_leads(self, limit: int = 10, offset: int = 0) -> Sequence[Lead]:
        stmt = (
            select(Lead)
            .order_by(Lead.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_recent_bookings(self, limit: int = 10, offset: int = 0) -> Sequence[Booking]:
        stmt = (
            select(Booking)
            .order_by(Booking.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_recent_payments(self, limit: int = 10, offset: int = 0) -> Sequence[Payment]:
        stmt = (
            select(Payment)
            .order_by(Payment.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    # ------------------------------------------------------------------
    # Top Rankings
    # ------------------------------------------------------------------

    async def get_top_agents(self, limit: int = 5) -> Sequence:
        # FIX: Payment.amount -> payment_amount, Payment.status ->
        # payment_status, COMPLETED -> SUCCESS.
        stmt = (
            select(
                User.id.label("agent_id"),
                User.full_name.label("agent_name"),
                func.count(func.distinct(Booking.id)).label("total_bookings"),
                func.coalesce(func.sum(Payment.payment_amount), 0).label(
                    "total_revenue_generated"
                ),
            )
            .select_from(User)
            .outerjoin(Booking, Booking.agent_id == User.id)
            .outerjoin(Payment, Payment.booking_id == Booking.id)
            .where(Payment.payment_status == PaymentStatus.SUCCESS.value)
            .group_by(User.id, User.full_name)
            .order_by(func.coalesce(func.sum(Payment.payment_amount), 0).desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return result.all()

    async def get_top_cities(self, limit: int = 5) -> Sequence:
        stmt = (
            select(
                Property.city.label("city"),
                func.count(func.distinct(Property.id)).label("total_properties"),
                func.count(func.distinct(Booking.id)).label("total_bookings"),
            )
            .select_from(Property)
            .outerjoin(Booking, Booking.property_id == Property.id)
            .group_by(Property.city)
            .order_by(func.count(func.distinct(Booking.id)).desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return result.all()

    async def get_top_properties(self, limit: int = 5) -> Sequence:
        # FIX: Payment.amount -> payment_amount, Payment.status ->
        # payment_status, COMPLETED -> SUCCESS.
        stmt = (
            select(
                Property.id.label("property_id"),
                Property.title.label("title"),
                Property.city.label("city"),
                func.count(func.distinct(Booking.id)).label("total_bookings"),
                func.coalesce(func.sum(Payment.payment_amount), 0).label("total_revenue"),
            )
            .select_from(Property)
            .outerjoin(Booking, Booking.property_id == Property.id)
            .outerjoin(Payment, Payment.booking_id == Booking.id)
            .where(Payment.payment_status == PaymentStatus.SUCCESS.value)
            .group_by(Property.id, Property.title, Property.city)
            .order_by(func.coalesce(func.sum(Payment.payment_amount), 0).desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return result.all()

    # ------------------------------------------------------------------
    # Follow-ups & Pending Payments
    # ------------------------------------------------------------------

    async def get_upcoming_followups(self, limit: int = 10, offset: int = 0) -> Sequence[Lead]:
        # FIX: Lead.follow_up_date doesn't exist -- real field is
        # next_follow_up. LeadStatusEnum.CONVERTED/LOST (lowercase, and
        # CONVERTED doesn't exist at all) -> LeadStatus.BOOKED/LOST
        # (uppercase, real members).
        today = date.today()
        stmt = (
            select(Lead)
            .where(
                Lead.next_follow_up.is_not(None),
                Lead.next_follow_up >= today,
                Lead.status.notin_(
                    [LeadStatus.BOOKED.value, LeadStatus.LOST.value]
                ),
            )
            .order_by(Lead.next_follow_up.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_pending_payments(self, limit: int = 10, offset: int = 0) -> Sequence[Payment]:
        # FIX: Payment.status -> payment_status; PENDING/PARTIAL values ->
        # real uppercase PaymentStatus members. Payment.due_date doesn't
        # exist on the Payment model at all -- ordered by payment_date
        # instead (the closest real, always-populated date field).
        stmt = (
            select(Payment)
            .where(
                Payment.payment_status.in_(
                    [PaymentStatus.PENDING.value, PaymentStatus.PARTIAL.value]
                )
            )
            .order_by(Payment.payment_date.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_outstanding_revenue(self) -> Decimal:
        stmt = select(func.coalesce(func.sum(Payment.payment_amount), 0)).where(
            Payment.payment_status.in_(
                [PaymentStatus.PENDING.value, PaymentStatus.PARTIAL.value]
            )
        )
        result = await self.db.execute(stmt)
        return result.scalar_one() or Decimal("0")