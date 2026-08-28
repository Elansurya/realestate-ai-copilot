# backend/app/schemas/payment.py

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.payment import PaymentStatus, PaymentMode, PaymentType


class PaymentBase(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    booking_id: uuid.UUID
    customer_id: uuid.UUID
    property_id: int
    received_by: Optional[int] = None
    payment_date: date
    payment_amount: Decimal = Field(..., gt=0, max_digits=14, decimal_places=2)
    payment_mode: PaymentMode
    transaction_reference: Optional[str] = Field(None, max_length=100)
    payment_type: PaymentType
    bank_name: Optional[str] = Field(None, max_length=150)
    cheque_number: Optional[str] = Field(None, max_length=50)
    remarks: Optional[str] = Field(None, max_length=500)


class PaymentCreate(PaymentBase):
    payment_status: PaymentStatus = PaymentStatus.PENDING
    receipt_number: Optional[str] = Field(None, max_length=30)

    @field_validator("payment_amount")
    @classmethod
    def validate_amount(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("payment_amount must be greater than zero")
        return v

    @model_validator(mode="after")
    def validate_mode_specific_fields(self) -> "PaymentCreate":
        if self.payment_mode == PaymentMode.CHEQUE and not self.cheque_number:
            raise ValueError("cheque_number is required when payment_mode is CHEQUE")
        if self.payment_mode == PaymentMode.BANK_TRANSFER and not self.bank_name:
            raise ValueError("bank_name is required when payment_mode is BANK_TRANSFER")
        return self


class PaymentUpdate(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    payment_date: Optional[date] = None
    payment_amount: Optional[Decimal] = Field(None, gt=0, max_digits=14, decimal_places=2)
    payment_mode: Optional[PaymentMode] = None
    transaction_reference: Optional[str] = Field(None, max_length=100)
    payment_status: Optional[PaymentStatus] = None
    payment_type: Optional[PaymentType] = None
    bank_name: Optional[str] = Field(None, max_length=150)
    cheque_number: Optional[str] = Field(None, max_length=50)
    remarks: Optional[str] = Field(None, max_length=500)
    receipt_number: Optional[str] = Field(None, max_length=30)
    is_active: Optional[bool] = None

    @model_validator(mode="after")
    def validate_mode_specific_fields(self) -> "PaymentUpdate":
        if self.payment_mode == PaymentMode.CHEQUE and not self.cheque_number:
            raise ValueError("cheque_number is required when payment_mode is CHEQUE")
        return self


class PaymentStatusUpdate(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    payment_status: PaymentStatus
    remarks: Optional[str] = Field(None, max_length=500)


class PaymentResponse(PaymentBase):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: uuid.UUID
    payment_number: str
    payment_status: PaymentStatus
    receipt_number: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PaymentListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total: int
    page: int
    page_size: int
    total_pages: int
    items: list[PaymentResponse]


class PaymentSearchFilter(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    booking_id: Optional[uuid.UUID] = None
    customer_id: Optional[uuid.UUID] = None
    property_id: Optional[int] = None
    received_by: Optional[int] = None
    payment_status: Optional[PaymentStatus] = None
    payment_mode: Optional[PaymentMode] = None
    payment_type: Optional[PaymentType] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    min_amount: Optional[Decimal] = None
    max_amount: Optional[Decimal] = None
    search: Optional[str] = Field(None, max_length=100)
    is_active: Optional[bool] = True
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)
    sort_by: str = Field("created_at")
    sort_order: str = Field("desc", pattern="^(asc|desc)$")

    @model_validator(mode="after")
    def validate_date_range(self) -> "PaymentSearchFilter":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must be earlier than or equal to date_to")
        return self

    @model_validator(mode="after")
    def validate_amount_range(self) -> "PaymentSearchFilter":
        if (
            self.min_amount is not None
            and self.max_amount is not None
            and self.min_amount > self.max_amount
        ):
            raise ValueError("min_amount must be less than or equal to max_amount")
        return self


class DashboardPaymentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_payments_count: int
    total_revenue: Decimal
    today_payments_count: int
    today_revenue: Decimal
    monthly_revenue: Decimal
    pending_amount: Decimal
    success_amount: Decimal
    failed_count: int
    refunded_amount: Decimal
    partial_count: int