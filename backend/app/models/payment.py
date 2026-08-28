# backend/app/models/payment.py

import uuid
import enum
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    String,
    Integer,
    Numeric,
    Boolean,
    ForeignKey,
    Index,
    CheckConstraint,
    DateTime,
    Date,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, ENUM as PGEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    PARTIAL = "PARTIAL"


class PaymentMode(str, enum.Enum):
    CASH = "CASH"
    UPI = "UPI"
    BANK_TRANSFER = "BANK_TRANSFER"
    CHEQUE = "CHEQUE"
    CARD = "CARD"
    OTHER = "OTHER"


class PaymentType(str, enum.Enum):
    TOKEN = "TOKEN"
    ADVANCE = "ADVANCE"
    INSTALLMENT = "INSTALLMENT"
    FULL_PAYMENT = "FULL_PAYMENT"
    REFUND = "REFUND"


payment_status_enum = PGEnum(
    PaymentStatus,
    name="payment_status_enum",
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)

payment_mode_enum = PGEnum(
    PaymentMode,
    name="payment_mode_enum",
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)

payment_type_enum = PGEnum(
    PaymentType,
    name="payment_type_enum",
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )

    payment_number: Mapped[str] = mapped_column(
        String(30),
        unique=True,
        nullable=False,
        index=True,
    )

    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("bookings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    property_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("properties.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    received_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    payment_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        server_default=func.current_date(),
    )

    payment_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )

    payment_mode: Mapped[PaymentMode] = mapped_column(
        payment_mode_enum,
        nullable=False,
    )

    transaction_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)

    payment_status: Mapped[PaymentStatus] = mapped_column(
        payment_status_enum,
        nullable=False,
        default=PaymentStatus.PENDING,
        server_default=PaymentStatus.PENDING.value,
    )

    payment_type: Mapped[PaymentType] = mapped_column(
        payment_type_enum,
        nullable=False,
    )

    bank_name: Mapped[str | None] = mapped_column(String(150), nullable=True)

    cheque_number: Mapped[str | None] = mapped_column(String(50), nullable=True)

    remarks: Mapped[str | None] = mapped_column(String(500), nullable=True)

    receipt_number: Mapped[str | None] = mapped_column(String(30), unique=True, nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    booking = relationship("Booking", back_populates="payments", lazy="joined")
    customer = relationship("Customer", back_populates="payments", lazy="joined")
    property = relationship("Property", back_populates="payments", lazy="joined")
    receiver = relationship("User", foreign_keys=[received_by], lazy="joined")

    __table_args__ = (
        CheckConstraint("payment_amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint(
            "payment_mode != 'CHEQUE' OR cheque_number IS NOT NULL",
            name="ck_payments_cheque_number_required",
        ),
        Index("ix_payments_booking_status", "booking_id", "payment_status"),
        Index("ix_payments_customer_date", "customer_id", "payment_date"),
        # NOTE: no explicit Index("ix_payments_property_id", "property_id")
        # here -- `property_id` above is declared with `index=True`, which
        # already creates that exact index under the same auto-generated
        # name, so declaring it again here duplicated it and broke schema
        # creation ("ix_payments_property_id already exists").
        Index("ix_payments_date_active", "payment_date", "is_active"),
    )

    def __repr__(self) -> str:
        return (
            f"<Payment {self.payment_number} "
            f"amount={self.payment_amount} status={self.payment_status}>"
        )