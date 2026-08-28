"""
Report Module - Repository
Enterprise Real Estate AI Copilot CRM

STRICTLY READ-ONLY.
This repository never performs INSERT, UPDATE or DELETE
operations. It only builds and executes SQLAlchemy 2.0
async SELECT / aggregate queries against Customer, Lead,
Property, Booking and Payment tables to power the Reports
module. No business logic lives here; it is composed
exclusively of query construction and execution.
"""

from typing import Any, Mapping, Optional, Sequence
from types import SimpleNamespace

from sqlalchemy import and_, case, distinct, extract, func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking
from app.models.customer import Customer
from app.models.lead import Lead, LeadStatus
from app.models.payment import Payment, PaymentStatus
from app.models.property import Property
from app.models.user import User
from app.schemas.report import ReportFilter


class ReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------
    # Internal filter helpers
    # ------------------------------------------------------------

    @staticmethod
    def _base_conditions(model: Any, date_column: Any, filters: Optional[ReportFilter]) -> list:
        conditions: list = []
        if filters is None:
            return conditions
        if filters.from_date is not None:
            conditions.append(date_column >= filters.from_date)
        if filters.to_date is not None:
            conditions.append(date_column <= filters.to_date)
        if filters.agent_id is not None:
            if hasattr(model, "agent_id"):
                conditions.append(model.agent_id == filters.agent_id)
            elif hasattr(model, "assigned_agent_id"):
                conditions.append(model.assigned_agent_id == filters.agent_id)
        if filters.city is not None and hasattr(model, "city"):
            conditions.append(model.city == filters.city)
        if filters.property_type is not None and hasattr(model, "property_type"):
            conditions.append(model.property_type == filters.property_type)
        return conditions

    # ------------------------------------------------------------
    # Compatibility / tabular report APIs
    # ------------------------------------------------------------
    # These methods expose the row-oriented repository contract used by the
    # report API tests while the aggregate methods below serve the typed
    # ReportService.  They intentionally use the real ORM column names and
    # return lightweight row objects with stable public attributes.

    async def get_revenue_report(self, *, start_date, end_date):
        stmt = (
            select(
                Payment.id.label("id"),
                Payment.payment_amount.label("amount"),
                Payment.payment_date.label("payment_date"),
                Payment.payment_status.label("status"),
            )
            .where(
                Payment.payment_date >= start_date,
                Payment.payment_date <= end_date,
                Payment.payment_status == PaymentStatus.SUCCESS,
            )
            .order_by(Payment.payment_date)
        )
        rows = (await self.session.execute(stmt)).mappings().all()
        return [SimpleNamespace(**dict(row)) for row in rows]

    async def get_booking_report(
        self, *, page=1, page_size=20, start_date=None, end_date=None,
        search=None, sort_by="created_at", sort_order="desc"
    ):
        conditions = []
        if start_date is not None:
            conditions.append(Booking.booking_date >= start_date)
        if end_date is not None:
            conditions.append(Booking.booking_date <= end_date)
        if search:
            pattern = f"%{search}%"
            conditions.append(
                select(Customer.id).where(
                    Customer.id == Booking.customer_id,
                    func.concat_ws(" ", Customer.first_name, Customer.middle_name, Customer.last_name).ilike(pattern),
                ).exists()
            )
        sort_column = {
            "created_at": Booking.created_at,
            "booking_date": Booking.booking_date,
            "amount": Booking.booking_amount,
            "booking_amount": Booking.booking_amount,
        }.get(sort_by, Booking.created_at)
        order = sort_column.desc() if str(sort_order).lower() == "desc" else sort_column.asc()
        base = select(Booking, Booking.booking_amount.label("amount")).where(and_(*conditions)).order_by(order)
        total = (await self.session.execute(select(func.count(Booking.id)).where(and_(*conditions)))).scalar_one()
        rows = (await self.session.execute(base.offset((max(page, 1)-1)*max(page_size,1)).limit(max(page_size,1)))).all()
        items = []
        for booking, amount in rows:
            data = {k: getattr(booking, k) for k in booking.__table__.columns.keys()}
            data["amount"] = amount
            items.append(SimpleNamespace(**data))
        return SimpleNamespace(items=items, total=int(total), page=max(page,1), page_size=max(page_size,1))

    async def get_payment_report(
        self, *, page=1, page_size=20, start_date=None, end_date=None,
        status=None, payment_method=None
    ):
        conditions = []
        if start_date is not None: conditions.append(Payment.payment_date >= start_date)
        if end_date is not None: conditions.append(Payment.payment_date <= end_date)
        if status is not None:
            status = PaymentStatus(status.strip().upper()) if isinstance(status, str) else status
            conditions.append(Payment.payment_status == status)
        if payment_method is not None:
            conditions.append(Payment.payment_mode == payment_method)
        total = (await self.session.execute(select(func.count(Payment.id)).where(and_(*conditions)))).scalar_one()
        stmt = select(Payment).where(and_(*conditions)).order_by(Payment.payment_date.desc()).offset((max(page,1)-1)*max(page_size,1)).limit(max(page_size,1))
        payments = (await self.session.execute(stmt)).scalars().all()
        items = []
        for payment in payments:
            data = {k: getattr(payment, k) for k in payment.__table__.columns.keys()}
            data["amount"] = payment.payment_amount
            data["status"] = payment.payment_status
            data["payment_method"] = payment.payment_mode
            items.append(SimpleNamespace(**data))
        return SimpleNamespace(items=items, total=int(total), page=max(page,1), page_size=max(page_size,1))

    async def get_lead_report(
        self, *, page=1, page_size=20, start_date=None, end_date=None, status=None, agent_id=None
    ):
        conditions = []
        if start_date is not None: conditions.append(Lead.created_at >= start_date)
        if end_date is not None: conditions.append(Lead.created_at <= end_date)
        if status is not None:
            status = LeadStatus(status.upper()) if isinstance(status, str) else status
            conditions.append(Lead.status == status)
        if agent_id is not None: conditions.append(Lead.assigned_agent_id == agent_id)
        total = (await self.session.execute(select(func.count(Lead.id)).where(and_(*conditions)))).scalar_one()
        stmt = select(Lead).where(and_(*conditions)).order_by(Lead.created_at.desc()).offset((max(page,1)-1)*max(page_size,1)).limit(max(page_size,1))
        leads = (await self.session.execute(stmt)).scalars().all()
        items = []
        for lead in leads:
            data = {k: getattr(lead, k) for k in lead.__table__.columns.keys()}
            data["agent_id"] = lead.assigned_agent_id
            items.append(SimpleNamespace(**data))
        return SimpleNamespace(items=items, total=int(total), page=max(page,1), page_size=max(page_size,1))

    async def get_lead_conversion_stats(self, *, start_date, end_date):
        result = await self.session.execute(
            select(
                func.count(Lead.id).label("total"),
                func.coalesce(func.sum(case((Lead.status == LeadStatus.BOOKED, 1), else_=0)), 0).label("converted"),
                func.coalesce(func.sum(case((Lead.status == LeadStatus.LOST, 1), else_=0)), 0).label("lost"),
            ).where(Lead.created_at >= start_date, Lead.created_at <= end_date)
        )
        return dict(result.mappings().one())

    async def get_customer_report(self, *, page=1, page_size=20, search=None):
        conditions = []
        if search:
            pattern = f"%{search}%"
            conditions.append(func.concat_ws(" ", Customer.first_name, Customer.middle_name, Customer.last_name).ilike(pattern))
        total = (await self.session.execute(select(func.count(Customer.id)).where(and_(*conditions)))).scalar_one()
        stmt = select(Customer).where(and_(*conditions)).order_by(Customer.created_at.desc()).offset((max(page,1)-1)*max(page_size,1)).limit(max(page_size,1))
        customers = (await self.session.execute(stmt)).scalars().all()
        return SimpleNamespace(items=list(customers), total=int(total), page=max(page,1), page_size=max(page_size,1))

    async def get_customer_analytics(self, *, customer_id):
        bookings = (await self.session.execute(select(func.count(Booking.id), func.coalesce(func.sum(Booking.booking_amount), 0)).where(Booking.customer_id == customer_id))).one()
        payments = (await self.session.execute(select(func.coalesce(func.sum(Payment.payment_amount), 0)).where(Payment.customer_id == customer_id, Payment.payment_status == PaymentStatus.SUCCESS))).scalar_one()
        return {"total_bookings": int(bookings[0] or 0), "total_booking_amount": bookings[1] or 0, "total_paid": payments or 0}

    async def get_property_report(self, *, page=1, page_size=20, status=None, city=None):
        conditions = []
        if status is not None: conditions.append(Property.property_status == status)
        if city is not None: conditions.append(Property.city == city)
        total = (await self.session.execute(select(func.count(Property.id)).where(and_(*conditions)))).scalar_one()
        stmt = select(Property).where(and_(*conditions)).order_by(Property.created_at.desc()).offset((max(page,1)-1)*max(page_size,1)).limit(max(page_size,1))
        properties = (await self.session.execute(stmt)).scalars().all()
        items = []
        for prop in properties:
            data = {k: getattr(prop, k) for k in prop.__table__.columns.keys()}
            data["status"] = prop.property_status
            items.append(SimpleNamespace(**data))
        return SimpleNamespace(items=items, total=int(total), page=max(page,1), page_size=max(page_size,1))

    async def get_dashboard_summary(self, *, start_date=None, end_date=None):
        filters = ReportFilter(from_date=start_date, to_date=end_date) if (start_date or end_date) else None
        return await self.get_business_summary_report(filters)

    async def get_export_dataset(self, *, report_type, start_date, end_date):
        kind = str(report_type).lower()
        if kind in {"revenue", "payment", "payments"}:
            return await self.get_revenue_report(start_date=start_date, end_date=end_date)
        if kind in {"booking", "bookings"}:
            page = await self.get_booking_report(page=1, page_size=10000, start_date=start_date, end_date=end_date)
            return page.items
        if kind in {"lead", "leads"}:
            page = await self.get_lead_report(page=1, page_size=10000, start_date=start_date, end_date=end_date)
            return page.items
        if kind in {"customer", "customers"}:
            page = await self.get_customer_report(page=1, page_size=10000)
            return page.items
        if kind in {"property", "properties"}:
            page = await self.get_property_report(page=1, page_size=10000)
            return page.items
        return []

    # ------------------------------------------------------------
    # Revenue Reports
    # ------------------------------------------------------------

    async def get_daily_revenue_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Payment, Payment.payment_date, filters)
        conditions.append(Payment.payment_status == PaymentStatus.SUCCESS)
        stmt = (
            select(
                func.date(Payment.payment_date).label("period_label"),
                func.coalesce(func.sum(Payment.payment_amount), 0).label("total_revenue"),
                func.count(distinct(Payment.booking_id)).label("total_bookings"),
                func.count(Payment.id).label("total_payments"),
            )
            .where(and_(*conditions))
            .group_by(func.date(Payment.payment_date))
            .order_by(func.date(Payment.payment_date))
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    async def get_weekly_revenue_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Payment, Payment.payment_date, filters)
        conditions.append(Payment.payment_status == PaymentStatus.SUCCESS)
        stmt = (
            select(
                extract("year", Payment.payment_date).label("year"),
                extract("week", Payment.payment_date).label("week"),
                func.coalesce(func.sum(Payment.payment_amount), 0).label("total_revenue"),
                func.count(distinct(Payment.booking_id)).label("total_bookings"),
                func.count(Payment.id).label("total_payments"),
            )
            .where(and_(*conditions))
            .group_by(
                extract("year", Payment.payment_date),
                extract("week", Payment.payment_date),
            )
            .order_by(
                extract("year", Payment.payment_date),
                extract("week", Payment.payment_date),
            )
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    async def get_monthly_revenue_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Payment, Payment.payment_date, filters)
        conditions.append(Payment.payment_status == PaymentStatus.SUCCESS)
        stmt = (
            select(
                extract("year", Payment.payment_date).label("year"),
                extract("month", Payment.payment_date).label("month"),
                func.coalesce(func.sum(Payment.payment_amount), 0).label("total_revenue"),
                func.count(distinct(Payment.booking_id)).label("total_bookings"),
            )
            .where(and_(*conditions))
            .group_by(
                extract("year", Payment.payment_date),
                extract("month", Payment.payment_date),
            )
            .order_by(
                extract("year", Payment.payment_date),
                extract("month", Payment.payment_date),
            )
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    async def get_yearly_revenue_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Payment, Payment.payment_date, filters)
        conditions.append(Payment.payment_status == PaymentStatus.SUCCESS)
        distinct_months = func.count(distinct(extract("month", Payment.payment_date)))
        stmt = (
            select(
                extract("year", Payment.payment_date).label("year"),
                func.coalesce(func.sum(Payment.payment_amount), 0).label("total_revenue"),
                func.count(distinct(Payment.booking_id)).label("total_bookings"),
                (
                    func.coalesce(func.sum(Payment.payment_amount), 0)
                    / func.nullif(distinct_months, 0)
                ).label("average_monthly_revenue"),
            )
            .where(and_(*conditions))
            .group_by(extract("year", Payment.payment_date))
            .order_by(extract("year", Payment.payment_date))
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    # ------------------------------------------------------------
    # Customer Reports
    # ------------------------------------------------------------

    async def get_customer_growth_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Customer, Customer.created_at, filters)
        stmt = (
            select(
                extract("year", Customer.created_at).label("year"),
                extract("month", Customer.created_at).label("month"),
                Customer.city.label("city"),
                func.count(Customer.id).label("new_customers"),
            )
            .where(and_(*conditions))
            .group_by(
                extract("year", Customer.created_at),
                extract("month", Customer.created_at),
                Customer.city,
            )
            .order_by(
                extract("year", Customer.created_at),
                extract("month", Customer.created_at),
            )
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    # ------------------------------------------------------------
    # Lead Reports
    # ------------------------------------------------------------

    async def get_lead_conversion_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Mapping[str, Any]:
        conditions = self._base_conditions(Lead, Lead.created_at, filters)
        stmt = select(
            func.count(Lead.id).label("total_leads"),
            func.coalesce(
                func.sum(case((Lead.status == "BOOKED", 1), else_=0)), 0
            ).label("converted_leads"),
            func.coalesce(
                func.sum(case((Lead.status == LeadStatus.LOST, 1), else_=0)), 0
            ).label("lost_leads"),
        ).where(and_(*conditions))
        result = await self.session.execute(stmt)
        return result.mappings().one()

    async def get_lead_source_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Lead, Lead.created_at, filters)
        stmt = (
            select(
                Lead.lead_source.label("source"),
                func.count(Lead.id).label("total_leads"),
                func.coalesce(
                    func.sum(case((Lead.status == "BOOKED", 1), else_=0)), 0
                ).label("converted_leads"),
            )
            .where(and_(*conditions))
            .group_by(Lead.lead_source)
            .order_by(func.count(Lead.id).desc())
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    # ------------------------------------------------------------
    # Property Reports
    # ------------------------------------------------------------

    async def get_property_status_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Property, Property.created_at, filters)
        stmt = (
            select(
                Property.property_status.label("status"),
                Property.city.label("city"),
                func.count(Property.id).label("total_properties"),
                func.coalesce(func.sum(Property.price), 0).label("total_value"),
                func.coalesce(func.avg(Property.price), 0).label("average_price"),
            )
            .where(and_(*conditions))
            .group_by(Property.property_status, Property.city)
            .order_by(func.count(Property.id).desc())
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    # ------------------------------------------------------------
    # Booking Reports
    # ------------------------------------------------------------

    async def get_booking_status_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Booking, Booking.booking_date, filters)
        if filters is not None and filters.booking_status:
            conditions.append(Booking.status == filters.booking_status)
        stmt = (
            select(
                Booking.status.label("status"),
                func.count(Booking.id).label("total_bookings"),
                func.coalesce(func.sum(Booking.booking_amount), 0).label("total_amount"),
            )
            .where(and_(*conditions))
            .group_by(Booking.status)
            .order_by(func.count(Booking.id).desc())
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    # ------------------------------------------------------------
    # Payment Reports
    # ------------------------------------------------------------

    async def get_payment_collection_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Payment, Payment.payment_date, filters)
        if filters is not None and filters.payment_status:
            conditions.append(Payment.payment_status == filters.payment_status)
        stmt = (
            select(
                Payment.payment_status.label("status"),
                func.count(Payment.id).label("total_payments"),
                func.coalesce(func.sum(Payment.payment_amount), 0).label("total_amount"),
                func.coalesce(
                    func.sum(
                        case(
                            (Payment.payment_status == PaymentStatus.REFUNDED, Payment.payment_amount),
                            else_=0,
                        )
                    ),
                    0,
                ).label("total_refunded"),
            )
            .where(and_(*conditions))
            .group_by(Payment.payment_status)
            .order_by(func.sum(Payment.payment_amount).desc())
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    async def get_pending_payment_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Payment, Payment.payment_date, filters)
        conditions.append(Payment.payment_status == PaymentStatus.PENDING)
        stmt = (
            select(
                Payment.id,
                Payment.booking_id,
                Payment.customer_id,
                Payment.payment_amount.label("amount"),
                Payment.payment_date,
            )
            .where(and_(*conditions))
            .order_by(Payment.payment_date.asc())
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    async def get_refund_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Mapping[str, Any]:
        conditions = self._base_conditions(Payment, Payment.payment_date, filters)
        conditions.append(Payment.payment_status == PaymentStatus.REFUNDED)
        stmt = select(
            func.count(Payment.id).label("total_refunds"),
            func.coalesce(func.sum(Payment.payment_amount), 0).label(
                "total_refund_amount"
            ),
        ).where(and_(*conditions))
        result = await self.session.execute(stmt)
        return result.mappings().one()

    # ------------------------------------------------------------
    # Agent Performance Report
    # ------------------------------------------------------------

    async def get_agent_performance_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        lead_conditions = self._base_conditions(Lead, Lead.created_at, filters)
        booking_conditions = self._base_conditions(Booking, Booking.booking_date, filters)

        lead_subq = (
            select(
                Lead.assigned_agent_id.label("agent_id"),
                func.count(Lead.id).label("total_leads"),
                func.coalesce(
                    func.sum(case((Lead.status == "BOOKED", 1), else_=0)), 0
                ).label("converted_leads"),
            )
            .where(and_(*lead_conditions))
            .group_by(Lead.assigned_agent_id)
            .subquery()
        )

        booking_subq = (
            select(
                Booking.agent_id.label("agent_id"),
                func.count(Booking.id).label("total_bookings"),
                func.coalesce(func.sum(Booking.booking_amount), 0).label("total_revenue"),
            )
            .where(and_(*booking_conditions))
            .group_by(Booking.agent_id)
            .subquery()
        )

        stmt = (
            select(
                User.id.label("agent_id"),
                User.full_name.label("agent_name"),
                func.coalesce(lead_subq.c.total_leads, 0).label("total_leads"),
                func.coalesce(lead_subq.c.converted_leads, 0).label(
                    "converted_leads"
                ),
                func.coalesce(booking_subq.c.total_bookings, 0).label(
                    "total_bookings"
                ),
                func.coalesce(booking_subq.c.total_revenue, 0).label(
                    "total_revenue"
                ),
            )
            .join(lead_subq, lead_subq.c.agent_id == User.id, isouter=True)
            .join(booking_subq, booking_subq.c.agent_id == User.id, isouter=True)
            .order_by(func.coalesce(booking_subq.c.total_revenue, 0).desc())
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    # ------------------------------------------------------------
    # Business Summary Report
    # ------------------------------------------------------------

    async def get_business_summary_report(
        self, filters: Optional[ReportFilter] = None
    ) -> Mapping[str, Any]:
        customer_conditions = self._base_conditions(Customer, Customer.created_at, filters)
        lead_conditions = self._base_conditions(Lead, Lead.created_at, filters)
        property_conditions = self._base_conditions(Property, Property.created_at, filters)
        booking_conditions = self._base_conditions(Booking, Booking.booking_date, filters)
        payment_conditions = self._base_conditions(Payment, Payment.payment_date, filters)

        total_customers_stmt = select(func.count(Customer.id)).where(
            and_(*customer_conditions)
        )
        total_leads_stmt = select(func.count(Lead.id)).where(and_(*lead_conditions))
        converted_leads_stmt = select(func.count(Lead.id)).where(
            and_(*lead_conditions, Lead.status == "BOOKED")
        )
        total_properties_stmt = select(func.count(Property.id)).where(
            and_(*property_conditions)
        )
        total_bookings_stmt = select(func.count(Booking.id)).where(
            and_(*booking_conditions)
        )
        total_revenue_stmt = select(
            func.coalesce(func.sum(Payment.payment_amount), 0)
        ).where(and_(*payment_conditions, Payment.payment_status == PaymentStatus.SUCCESS))
        pending_payments_stmt = select(
            func.coalesce(func.sum(Payment.payment_amount), 0)
        ).where(and_(*payment_conditions, Payment.payment_status == PaymentStatus.PENDING))
        refunds_stmt = select(
            func.coalesce(func.sum(Payment.payment_amount), 0)
        ).where(and_(*payment_conditions, Payment.payment_status == PaymentStatus.REFUNDED))

        total_customers = (await self.session.execute(total_customers_stmt)).scalar_one()
        total_leads = (await self.session.execute(total_leads_stmt)).scalar_one()
        converted_leads = (await self.session.execute(converted_leads_stmt)).scalar_one()
        total_properties = (await self.session.execute(total_properties_stmt)).scalar_one()
        total_bookings = (await self.session.execute(total_bookings_stmt)).scalar_one()
        total_revenue = (await self.session.execute(total_revenue_stmt)).scalar_one()
        total_pending_payments = (
            await self.session.execute(pending_payments_stmt)
        ).scalar_one()
        total_refunds = (await self.session.execute(refunds_stmt)).scalar_one()

        conversion_rate = (
            float(converted_leads) / float(total_leads) * 100 if total_leads else 0.0
        )

        return {
            "total_customers": total_customers,
            "total_leads": total_leads,
            "total_properties": total_properties,
            "total_bookings": total_bookings,
            "total_revenue": total_revenue,
            "total_pending_payments": total_pending_payments,
            "total_refunds": total_refunds,
            "conversion_rate": conversion_rate,
        }

    # ------------------------------------------------------------
    # Top N Reports
    # ------------------------------------------------------------

    async def get_top_customers(
        self, filters: Optional[ReportFilter] = None, limit: int = 10
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Booking, Booking.booking_date, filters)
        stmt = (
            select(
                Customer.id.label("customer_id"),
                func.concat_ws(
                    " ", Customer.first_name, Customer.middle_name, Customer.last_name
                ).label("customer_name"),
                func.count(Booking.id).label("total_bookings"),
                func.coalesce(func.sum(Booking.booking_amount), 0).label("total_spent"),
            )
            .join(Booking, Booking.customer_id == Customer.id)
            .where(and_(*conditions))
            .group_by(
                Customer.id, Customer.first_name, Customer.middle_name, Customer.last_name
            )
            .order_by(func.sum(Booking.booking_amount).desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    async def get_top_agents(
        self, filters: Optional[ReportFilter] = None, limit: int = 10
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Booking, Booking.booking_date, filters)
        stmt = (
            select(
                User.id.label("agent_id"),
                User.full_name.label("agent_name"),
                func.coalesce(func.sum(Booking.booking_amount), 0).label("total_revenue"),
                func.count(Booking.id).label("total_bookings"),
            )
            .join(Booking, Booking.agent_id == User.id)
            .where(and_(*conditions))
            .group_by(User.id, User.full_name)
            .order_by(func.sum(Booking.booking_amount).desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    async def get_top_properties(
        self, filters: Optional[ReportFilter] = None, limit: int = 10
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Booking, Booking.booking_date, filters)
        stmt = (
            select(
                Property.id.label("property_id"),
                Property.title.label("title"),
                Property.city.label("city"),
                func.count(Booking.id).label("total_bookings"),
                func.coalesce(func.sum(Booking.booking_amount), 0).label("total_revenue"),
            )
            .join(Booking, Booking.property_id == Property.id)
            .where(and_(*conditions))
            .group_by(Property.id, Property.title, Property.city)
            .order_by(func.sum(Booking.booking_amount).desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    async def get_top_cities(
        self, filters: Optional[ReportFilter] = None, limit: int = 10
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Booking, Booking.booking_date, filters)
        stmt = (
            select(
                Customer.city.label("city"),
                func.count(distinct(Customer.id)).label("total_customers"),
                func.count(Booking.id).label("total_bookings"),
                func.coalesce(func.sum(Booking.booking_amount), 0).label("total_revenue"),
            )
            .join(Booking, Booking.customer_id == Customer.id)
            .where(and_(*conditions))
            .group_by(Customer.city)
            .order_by(func.sum(Booking.booking_amount).desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    # ------------------------------------------------------------
    # Trend Reports
    # ------------------------------------------------------------

    async def get_monthly_trend(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Payment, Payment.payment_date, filters)
        conditions.append(Payment.payment_status == PaymentStatus.SUCCESS)
        stmt = (
            select(
                extract("year", Payment.payment_date).label("year"),
                extract("month", Payment.payment_date).label("month"),
                func.coalesce(func.sum(Payment.payment_amount), 0).label("total_revenue"),
                func.count(distinct(Payment.booking_id)).label("total_bookings"),
            )
            .where(and_(*conditions))
            .group_by(
                extract("year", Payment.payment_date),
                extract("month", Payment.payment_date),
            )
            .order_by(
                extract("year", Payment.payment_date),
                extract("month", Payment.payment_date),
            )
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()

    async def get_yearly_trend(
        self, filters: Optional[ReportFilter] = None
    ) -> Sequence[RowMapping]:
        conditions = self._base_conditions(Payment, Payment.payment_date, filters)
        conditions.append(Payment.payment_status == PaymentStatus.SUCCESS)
        stmt = (
            select(
                extract("year", Payment.payment_date).label("year"),
                func.coalesce(func.sum(Payment.payment_amount), 0).label("total_revenue"),
                func.count(distinct(Payment.booking_id)).label("total_bookings"),
            )
            .where(and_(*conditions))
            .group_by(extract("year", Payment.payment_date))
            .order_by(extract("year", Payment.payment_date))
        )
        result = await self.session.execute(stmt)
        return result.mappings().all()