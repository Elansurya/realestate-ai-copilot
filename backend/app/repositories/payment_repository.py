# backend/app/repositories/payment_repository.py

import uuid
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

from sqlalchemy import select, func, and_, or_, extract
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment, PaymentStatus
from app.schemas.payment import PaymentCreate, PaymentUpdate, PaymentSearchFilter


class PaymentRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, payment_data: PaymentCreate, payment_number: str) -> Payment:
        payment = Payment(
            payment_number=payment_number,
            **payment_data.model_dump(),
        )
        self.db.add(payment)
        await self.db.flush()
        await self.db.refresh(payment)
        return payment

    async def get_by_id(self, payment_id: uuid.UUID) -> Optional[Payment]:
        stmt = select(Payment).where(
            Payment.id == payment_id,
            Payment.is_active.is_(True),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_any_status(self, payment_id: uuid.UUID) -> Optional[Payment]:
        stmt = select(Payment).where(Payment.id == payment_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_payment_number(self, payment_number: str) -> Optional[Payment]:
        stmt = select(Payment).where(Payment.payment_number == payment_number)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_last_payment_number(self, year: int) -> Optional[str]:
        prefix = f"PAY-{year}-"
        stmt = (
            select(Payment.payment_number)
            .where(Payment.payment_number.like(f"{prefix}%"))
            .order_by(Payment.payment_number.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def update(
        self, payment_id: uuid.UUID, payment_data: PaymentUpdate
    ) -> Optional[Payment]:
        payment = await self.get_by_id(payment_id)
        if not payment:
            return None
        update_data = payment_data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(payment, field, value)
        await self.db.flush()
        await self.db.refresh(payment)
        return payment

    async def update_status(
        self,
        payment_id: uuid.UUID,
        status_: PaymentStatus,
        remarks: Optional[str] = None,
    ) -> Optional[Payment]:
        payment = await self.get_by_id(payment_id)
        if not payment:
            return None
        payment.payment_status = status_
        if remarks is not None:
            payment.remarks = remarks
        await self.db.flush()
        await self.db.refresh(payment)
        return payment

    async def soft_delete(self, payment_id: uuid.UUID) -> bool:
        payment = await self.get_by_id(payment_id)
        if not payment:
            return False
        payment.is_active = False
        await self.db.flush()
        return True

    async def restore(self, payment_id: uuid.UUID) -> bool:
        payment = await self.get_by_id_any_status(payment_id)
        if not payment:
            return False
        payment.is_active = True
        await self.db.flush()
        return True

    def _build_filters(self, filters: PaymentSearchFilter) -> list:
        conditions = []

        if filters.is_active is not None:
            conditions.append(Payment.is_active.is_(filters.is_active))
        if filters.booking_id:
            conditions.append(Payment.booking_id == filters.booking_id)
        if filters.customer_id:
            conditions.append(Payment.customer_id == filters.customer_id)
        if filters.property_id:
            conditions.append(Payment.property_id == filters.property_id)
        if filters.received_by:
            conditions.append(Payment.received_by == filters.received_by)
        if filters.payment_status:
            conditions.append(Payment.payment_status == filters.payment_status)
        if filters.payment_mode:
            conditions.append(Payment.payment_mode == filters.payment_mode)
        if filters.payment_type:
            conditions.append(Payment.payment_type == filters.payment_type)
        if filters.date_from:
            conditions.append(Payment.payment_date >= filters.date_from)
        if filters.date_to:
            conditions.append(Payment.payment_date <= filters.date_to)
        if filters.min_amount is not None:
            conditions.append(Payment.payment_amount >= filters.min_amount)
        if filters.max_amount is not None:
            conditions.append(Payment.payment_amount <= filters.max_amount)
        if filters.search:
            search_term = f"%{filters.search}%"
            conditions.append(
                or_(
                    Payment.payment_number.ilike(search_term),
                    Payment.transaction_reference.ilike(search_term),
                    Payment.receipt_number.ilike(search_term),
                    Payment.cheque_number.ilike(search_term),
                )
            )
        return conditions

    async def search(
        self, filters: PaymentSearchFilter
    ) -> tuple[Sequence[Payment], int]:
        conditions = self._build_filters(filters)
        where_clause = and_(*conditions) if conditions else True

        count_stmt = select(func.count()).select_from(Payment).where(where_clause)
        total = (await self.db.execute(count_stmt)).scalar_one()

        sort_column = getattr(Payment, filters.sort_by, Payment.created_at)
        order_expr = (
            sort_column.desc() if filters.sort_order == "desc" else sort_column.asc()
        )

        stmt = (
            select(Payment)
            .where(where_clause)
            .order_by(order_expr)
            .offset((filters.page - 1) * filters.page_size)
            .limit(filters.page_size)
        )
        result = await self.db.execute(stmt)
        items = result.scalars().all()
        return items, total

    async def get_by_booking_id(self, booking_id: uuid.UUID) -> Sequence[Payment]:
        stmt = (
            select(Payment)
            .where(Payment.booking_id == booking_id, Payment.is_active.is_(True))
            .order_by(Payment.payment_date.asc())
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_by_customer_id(self, customer_id: uuid.UUID) -> Sequence[Payment]:
        stmt = (
            select(Payment)
            .where(Payment.customer_id == customer_id, Payment.is_active.is_(True))
            .order_by(Payment.payment_date.desc())
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_by_property_id(self, property_id: int) -> Sequence[Payment]:
        stmt = (
            select(Payment)
            .where(Payment.property_id == property_id, Payment.is_active.is_(True))
            .order_by(Payment.payment_date.desc())
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_total_paid_for_booking(self, booking_id: uuid.UUID) -> Decimal:
        stmt = select(func.coalesce(func.sum(Payment.payment_amount), 0)).where(
            Payment.booking_id == booking_id,
            Payment.payment_status == PaymentStatus.SUCCESS,
            Payment.is_active.is_(True),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one()

    async def get_today_payments(self) -> Sequence[Payment]:
        today = date.today()
        stmt = (
            select(Payment)
            .where(Payment.payment_date == today, Payment.is_active.is_(True))
            .order_by(Payment.created_at.desc())
        )
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_today_summary(self) -> tuple[int, Decimal]:
        today = date.today()
        stmt = select(
            func.count(Payment.id),
            func.coalesce(func.sum(Payment.payment_amount), 0),
        ).where(
            Payment.payment_date == today,
            Payment.payment_status == PaymentStatus.SUCCESS,
            Payment.is_active.is_(True),
        )
        result = await self.db.execute(stmt)
        count, total = result.one()
        return count, total

    async def get_monthly_revenue(self, year: int, month: int) -> Decimal:
        stmt = select(func.coalesce(func.sum(Payment.payment_amount), 0)).where(
            extract("year", Payment.payment_date) == year,
            extract("month", Payment.payment_date) == month,
            Payment.payment_status == PaymentStatus.SUCCESS,
            Payment.is_active.is_(True),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one()

    async def get_dashboard_summary(self) -> dict:
        today = date.today()

        total_stmt = select(
            func.count(Payment.id),
            func.coalesce(func.sum(Payment.payment_amount), 0),
        ).where(Payment.is_active.is_(True))
        total_count, total_revenue = (await self.db.execute(total_stmt)).one()

        today_count, today_revenue = await self.get_today_summary()

        monthly_revenue = await self.get_monthly_revenue(today.year, today.month)

        pending_stmt = select(func.coalesce(func.sum(Payment.payment_amount), 0)).where(
            Payment.payment_status == PaymentStatus.PENDING,
            Payment.is_active.is_(True),
        )
        pending_amount = (await self.db.execute(pending_stmt)).scalar_one()

        success_stmt = select(func.coalesce(func.sum(Payment.payment_amount), 0)).where(
            Payment.payment_status == PaymentStatus.SUCCESS,
            Payment.is_active.is_(True),
        )
        success_amount = (await self.db.execute(success_stmt)).scalar_one()

        failed_stmt = select(func.count(Payment.id)).where(
            Payment.payment_status == PaymentStatus.FAILED,
            Payment.is_active.is_(True),
        )
        failed_count = (await self.db.execute(failed_stmt)).scalar_one()

        refunded_stmt = select(func.coalesce(func.sum(Payment.payment_amount), 0)).where(
            Payment.payment_status == PaymentStatus.REFUNDED,
            Payment.is_active.is_(True),
        )
        refunded_amount = (await self.db.execute(refunded_stmt)).scalar_one()

        partial_stmt = select(func.count(Payment.id)).where(
            Payment.payment_status == PaymentStatus.PARTIAL,
            Payment.is_active.is_(True),
        )
        partial_count = (await self.db.execute(partial_stmt)).scalar_one()

        return {
            "total_payments_count": total_count,
            "total_revenue": total_revenue,
            "today_payments_count": today_count,
            "today_revenue": today_revenue,
            "monthly_revenue": monthly_revenue,
            "pending_amount": pending_amount,
            "success_amount": success_amount,
            "failed_count": failed_count,
            "refunded_amount": refunded_amount,
            "partial_count": partial_count,
        }