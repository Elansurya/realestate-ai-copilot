"""
backend/app/schemas/customer.py

Pydantic v2 schemas for the Customer module.

Generated exclusively from the approved `app.models.customer.Customer`
ORM model (finalized version). No fields, types, or defaults are assumed
or carried over from any prior schema revision.

Responsibilities:
    - Define request/response contracts for customer creation, partial
      updates, single-record retrieval, paginated listing, search/filter
      query parameters, CSV/XLSX export requests, and aggregate
      statistics reporting.

Design Notes:
    - These schemas are transport/validation contracts only; they
      contain no business logic or database access.
    - `CustomerType`, `CustomerStatus`, `CustomerSource`, `Gender`,
      `MaritalStatus`, `PreferredPropertyType`, and `PreferredBHK` are
      imported directly from `app.models.customer` and reused as-is, so
      API-facing enum values always stay in lockstep with the ORM/DB
      enum definitions with zero duplication (mirrors
      `app/schemas/lead.py`'s reuse of `LeadStatus`/`LeadPriority`/
      `LeadSource`).
    - `ConfigDict(from_attributes=True)` is used on all response
      schemas so they can be constructed directly from `Customer` ORM
      instances without manual dict conversion.
    - `assigned_to_id`, `created_by_id`, and `updated_by_id` are typed
      as `int`, NOT `uuid.UUID` — the approved model declares these as
      `Integer` foreign keys against `users.id`, which is itself an
      autoincrementing `Integer` primary key (see `app/models/user.py`).
      `id` and `lead_id` remain `uuid.UUID`, matching the model's
      PostgreSQL UUID columns.
    - Regex/format validation (PAN, masked Aadhaar, postal code
      non-blank) mirrors the `CheckConstraint`s declared in
      `Customer.__table_args__` exactly, so a payload that passes schema
      validation will never be rejected by the database's own
      constraints. Phone and passport-number formats have no DB-level
      CHECK constraint in the approved model; the patterns below are
      supplementary API-layer validation only.
    - Whitespace-only or empty-string input on optional free-text
      fields is normalized to `None` at the schema boundary, so the
      service/repository layers never have to special-case empty
      strings vs. NULL.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.models.customer import (
    CustomerSource,
    CustomerStatus,
    CustomerType,
    Gender,
    MaritalStatus,
    PreferredBHK,
    PreferredPropertyType,
)

# --------------------------------------------------------------------------
# Shared Validation Constants
# --------------------------------------------------------------------------

#: Mirrors `ck_customers_pan_number_format` exactly.
_PAN_REGEX = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]{1}$")

#: Mirrors `ck_customers_aadhaar_number_masked_format` exactly. Only the
#: pre-masked representation is ever accepted; this schema never accepts,
#: stores, or transmits a full/unmasked Aadhaar number.
_AADHAAR_MASKED_REGEX = re.compile(r"^[Xx]{4}-[Xx]{4}-[0-9]{4}$")

#: Supplementary API-layer format for `phone`/`alternate_phone`. No
#: equivalent CHECK constraint exists on the approved model.
_PHONE_REGEX = re.compile(r"^\+?[1-9]\d{7,14}$")

#: Supplementary API-layer format for `passport_number`. No equivalent
#: CHECK constraint exists on the approved model.
_PASSPORT_REGEX = re.compile(r"^[A-Z0-9]{6,9}$")


# --------------------------------------------------------------------------
# Reusable Validator Functions
# --------------------------------------------------------------------------
def _blank_to_none(value: Optional[str]) -> Optional[str]:
    """Strips surrounding whitespace and converts a blank string to `None`."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _validate_phone(value: Optional[str]) -> Optional[str]:
    """Validates `phone`/`alternate_phone` against the accepted pattern."""
    value = _blank_to_none(value)
    if value is None:
        return None
    if not _PHONE_REGEX.match(value):
        raise ValueError(
            "Phone number must contain 8-15 digits, optionally prefixed "
            "with '+', and must not start with 0 (e.g. '+919876543210')."
        )
    return value


def _validate_pan_number(value: Optional[str]) -> Optional[str]:
    """Validates and uppercases `pan_number` per `ck_customers_pan_number_format`."""
    value = _blank_to_none(value)
    if value is None:
        return None
    value = value.upper()
    if not _PAN_REGEX.match(value):
        raise ValueError(
            "PAN number must follow the format 'AAAAA9999A' "
            "(5 uppercase letters, 4 digits, 1 uppercase letter)."
        )
    return value


def _validate_aadhaar_number(value: Optional[str]) -> Optional[str]:
    """Validates `aadhaar_number` is supplied in pre-masked form only."""
    value = _blank_to_none(value)
    if value is None:
        return None
    if not _AADHAAR_MASKED_REGEX.match(value):
        raise ValueError(
            "Aadhaar number must be supplied in masked format "
            "'XXXX-XXXX-1234' (first 8 digits masked with 'X'). Full, "
            "unmasked Aadhaar numbers are never accepted by this API."
        )
    masked_part, digit_part = value.rsplit("-", 1)
    return f"{masked_part.upper()}-{digit_part}"


def _validate_passport_number(value: Optional[str]) -> Optional[str]:
    """Validates and uppercases `passport_number`."""
    value = _blank_to_none(value)
    if value is None:
        return None
    value = value.upper()
    if not _PASSPORT_REGEX.match(value):
        raise ValueError("Passport number must be 6-9 alphanumeric characters.")
    return value


def _validate_postal_code(value: Optional[str]) -> Optional[str]:
    """
    Validates `postal_code` is non-blank after stripping, matching
    `ck_customers_postal_code_not_blank` exactly. No stricter format is
    enforced here since the DB constraint itself imposes none.
    """
    return _blank_to_none(value)


def _validate_non_negative(value: Optional[Decimal]) -> Optional[Decimal]:
    """Validates a monetary field is >= 0, matching the corresponding CHECK constraint."""
    if value is not None and value < 0:
        raise ValueError("Value must be greater than or equal to 0.")
    return value


def _validate_dob_not_future(value: Optional[date]) -> Optional[date]:
    """
    Validates `date_of_birth` is not in the future. This is a
    reasonable API-time proxy for `ck_customers_dob_not_after_creation`
    (which compares against `created_at::date` at the database level).
    """
    if value is not None and value > date.today():
        raise ValueError("Date of birth cannot be in the future.")
    return value


def _validate_optional_text(value: Optional[str]) -> Optional[str]:
    """Generic optional free-text normalizer: strip whitespace, blank -> None."""
    return _blank_to_none(value)


# --------------------------------------------------------------------------
# Customer Base Schema
# --------------------------------------------------------------------------
class CustomerBase(BaseModel):
    """
    Common, client-writable fields shared across customer creation,
    update, and response schemas. Excludes identity (`id`) and
    server/audit-managed fields (`created_by_id`, `updated_by_id`,
    `last_contacted_at`, `created_at`, `updated_at`), which are added
    only in `CustomerResponse`.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    # ---- Personal Information -----------------------------------------
    first_name: str = Field(
        ...,
        max_length=100,
        description="Customer's given name.",
        examples=["Rohan"],
    )
    middle_name: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Customer's middle name, if applicable.",
        examples=["Kumar"],
    )
    last_name: str = Field(
        ...,
        max_length=100,
        description="Customer's family name.",
        examples=["Sharma"],
    )
    date_of_birth: Optional[date] = Field(
        default=None,
        description="Customer's date of birth. Cannot be in the future.",
        examples=["1990-05-14"],
    )
    gender: Optional[Gender] = Field(
        default=None,
        description="Customer's self-identified gender.",
        examples=["MALE"],
    )
    marital_status: Optional[MaritalStatus] = Field(
        default=None,
        description="Customer's marital status.",
        examples=["MARRIED"],
    )
    nationality: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Customer's nationality.",
        examples=["Indian"],
    )

    # ---- Contact Information --------------------------------------------
    email: EmailStr = Field(
        ...,
        description="Unique contact email address for the customer.",
        examples=["rohan.kumar@example.com"],
    )
    phone: str = Field(
        ...,
        max_length=20,
        description="Primary contact phone number.",
        examples=["+919876543210"],
    )
    alternate_phone: Optional[str] = Field(
        default=None,
        max_length=20,
        description="Secondary/alternate contact phone number.",
        examples=["+919812345670"],
    )

    # ---- Professional Information ----------------------------------------
    occupation: Optional[str] = Field(
        default=None,
        max_length=150,
        description="Customer's occupation/profession.",
        examples=["Software Engineer"],
    )
    company_name: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Name of the customer's employer or company.",
        examples=["Acme Technologies Pvt. Ltd."],
    )
    annual_income: Optional[Decimal] = Field(
        default=None,
        description="Declared annual income, used for financial qualification (must be >= 0).",
        examples=[1800000],
    )

    # ---- KYC (Know Your Customer) ----------------------------------------
    pan_number: Optional[str] = Field(
        default=None,
        max_length=10,
        description="Government-issued PAN, format 'AAAAA9999A'.",
        examples=["ABCDE1234F"],
    )
    aadhaar_number: Optional[str] = Field(
        default=None,
        max_length=14,
        description=(
            "Masked Aadhaar representation only ('XXXX-XXXX-1234'). Full, "
            "unmasked Aadhaar numbers are never accepted."
        ),
        examples=["XXXX-XXXX-1234"],
    )
    passport_number: Optional[str] = Field(
        default=None,
        max_length=20,
        description="Passport number, if provided.",
        examples=["N1234567"],
    )

    # ---- Address -----------------------------------------------------------
    address_line_1: Optional[str] = Field(default=None, max_length=255)
    address_line_2: Optional[str] = Field(default=None, max_length=255)
    landmark: Optional[str] = Field(default=None, max_length=150)
    city: Optional[str] = Field(default=None, max_length=100, examples=["Bengaluru"])
    state: Optional[str] = Field(default=None, max_length=100, examples=["Karnataka"])
    country: Optional[str] = Field(default=None, max_length=100, examples=["India"])
    postal_code: Optional[str] = Field(default=None, max_length=20, examples=["560103"])

    # ---- Customer Preferences ------------------------------------------------
    budget_min: Optional[Decimal] = Field(
        default=None,
        description="Minimum stated budget for the customer's property search (must be >= 0).",
        examples=[5000000],
    )
    budget_max: Optional[Decimal] = Field(
        default=None,
        description=(
            "Maximum stated budget for the customer's property search "
            "(must be >= 0 and >= budget_min)."
        ),
        examples=[7500000],
    )
    preferred_city: Optional[str] = Field(default=None, max_length=100, examples=["Bengaluru"])
    preferred_area: Optional[str] = Field(default=None, max_length=150, examples=["Whitefield"])
    preferred_property_type: Optional[PreferredPropertyType] = Field(
        default=None,
        description="Type of property the customer is primarily interested in.",
        examples=["APARTMENT"],
    )
    preferred_bhk: Optional[PreferredBHK] = Field(
        default=None,
        description="Room configuration (BHK) the customer prefers.",
        examples=["THREE_BHK"],
    )

    # ---- Classification / Lifecycle ------------------------------------------
    customer_type: CustomerType = Field(
        default=CustomerType.BUYER,
        description="Commercial role the customer plays in a transaction.",
        examples=["BUYER"],
    )
    customer_source: CustomerSource = Field(
        default=CustomerSource.OTHER,
        description="Acquisition channel through which the customer was onboarded.",
        examples=["WEBSITE"],
    )
    status: CustomerStatus = Field(
        default=CustomerStatus.ACTIVE,
        description="Lifecycle status of the customer record within the CRM.",
        examples=["ACTIVE"],
    )
    notes: Optional[str] = Field(
        default=None,
        description="Free-form notes recorded by agents about this customer.",
    )
    is_active: bool = Field(
        default=True,
        description="Soft-disable flag; inactive customers are excluded from active views.",
    )

    # ---- Relationship Foreign Keys -------------------------------------------
    lead_id: Optional[uuid.UUID] = Field(
        default=None,
        description="Optional reference to the originating Lead record.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    assigned_to_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User (agent) responsible for this customer.",
        examples=[12],
    )

    # ---- Follow-up -----------------------------------------------------------
    next_followup_date: Optional[date] = Field(
        default=None,
        description="Scheduled date for the next follow-up action with this customer.",
        examples=["2026-08-01"],
    )


# --------------------------------------------------------------------------
# Shared Validator Mixin (Create + Update)
# --------------------------------------------------------------------------
class _CustomerValidatorMixin(BaseModel):
    """
    Validator attachments identical across `CustomerCreate` and
    `CustomerUpdate`, factored out to a single definition so the two
    request schemas cannot drift out of sync.

    `check_fields=False` is required on every attachment here because
    this mixin declares no fields of its own — each field only exists
    on the concrete subclass (`CustomerCreate`/`CustomerUpdate`) that
    mixes this class in.

    Deliberately excluded (kept on each subclass individually, since
    they differ or are subclass-specific):
        - `phone`: `CustomerCreate` validates it in `mode="after"`
          (the field is required/non-optional there); `CustomerUpdate`
          validates it in `mode="before"` (the field is optional
          there).
        - `CustomerUpdate._validate_not_empty`: has no equivalent in
          `CustomerCreate`.
    """

    _validate_middle_name = field_validator("middle_name", mode="before", check_fields=False)(
        _validate_optional_text
    )
    _validate_nationality = field_validator("nationality", mode="before", check_fields=False)(
        _validate_optional_text
    )
    _validate_occupation = field_validator("occupation", mode="before", check_fields=False)(
        _validate_optional_text
    )
    _validate_company_name = field_validator("company_name", mode="before", check_fields=False)(
        _validate_optional_text
    )
    _validate_address_line_1 = field_validator("address_line_1", mode="before", check_fields=False)(
        _validate_optional_text
    )
    _validate_address_line_2 = field_validator("address_line_2", mode="before", check_fields=False)(
        _validate_optional_text
    )
    _validate_landmark = field_validator("landmark", mode="before", check_fields=False)(
        _validate_optional_text
    )
    _validate_city = field_validator("city", mode="before", check_fields=False)(_validate_optional_text)
    _validate_state = field_validator("state", mode="before", check_fields=False)(_validate_optional_text)
    _validate_country = field_validator("country", mode="before", check_fields=False)(
        _validate_optional_text
    )
    _validate_preferred_city = field_validator("preferred_city", mode="before", check_fields=False)(
        _validate_optional_text
    )
    _validate_preferred_area = field_validator("preferred_area", mode="before", check_fields=False)(
        _validate_optional_text
    )
    _validate_notes = field_validator("notes", mode="before", check_fields=False)(_validate_optional_text)

    _validate_alt_phone_fmt = field_validator("alternate_phone", mode="before", check_fields=False)(
        _validate_phone
    )
    _validate_pan_fmt = field_validator("pan_number", mode="before", check_fields=False)(
        _validate_pan_number
    )
    _validate_aadhaar_fmt = field_validator("aadhaar_number", mode="before", check_fields=False)(
        _validate_aadhaar_number
    )
    _validate_passport_fmt = field_validator("passport_number", mode="before", check_fields=False)(
        _validate_passport_number
    )
    _validate_postal_fmt = field_validator("postal_code", mode="before", check_fields=False)(
        _validate_postal_code
    )

    _validate_annual_income_nn = field_validator("annual_income", mode="after", check_fields=False)(
        _validate_non_negative
    )
    _validate_budget_min_nn = field_validator("budget_min", mode="after", check_fields=False)(
        _validate_non_negative
    )
    _validate_budget_max_nn = field_validator("budget_max", mode="after", check_fields=False)(
        _validate_non_negative
    )
    _validate_dob = field_validator("date_of_birth", mode="after", check_fields=False)(
        _validate_dob_not_future
    )

    @model_validator(mode="after")
    def _validate_budget_range(self) -> "_CustomerValidatorMixin":
        """Enforces `ck_customers_budget_max_gte_min`: budget_max >= budget_min."""
        if (
            self.budget_min is not None
            and self.budget_max is not None
            and self.budget_max < self.budget_min
        ):
            raise ValueError("budget_max must be greater than or equal to budget_min.")
        return self


# --------------------------------------------------------------------------
# Customer Create Schema
# --------------------------------------------------------------------------
class CustomerCreate(CustomerBase, _CustomerValidatorMixin):
    """
    Request payload for creating a new customer.

    Overrides `first_name`/`last_name` from `CustomerBase` to enforce a
    non-empty minimum length, and attaches format/range validators for
    every field with a corresponding CHECK constraint or KYC/contact
    format requirement. Server-managed fields (`id`, timestamps,
    `created_by_id`, `updated_by_id`) are deliberately excluded and are
    populated by the service layer from the authenticated request
    context.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "first_name": "Rohan",
                "middle_name": "Kumar",
                "last_name": "Sharma",
                "date_of_birth": "1990-05-14",
                "gender": "MALE",
                "marital_status": "MARRIED",
                "nationality": "Indian",
                "email": "rohan.kumar@example.com",
                "phone": "+919876543210",
                "alternate_phone": "+919812345670",
                "occupation": "Software Engineer",
                "company_name": "Acme Technologies Pvt. Ltd.",
                "annual_income": 1800000,
                "pan_number": "ABCDE1234F",
                "aadhaar_number": "XXXX-XXXX-1234",
                "passport_number": "N1234567",
                "address_line_1": "12th Main Road",
                "city": "Bengaluru",
                "state": "Karnataka",
                "country": "India",
                "postal_code": "560103",
                "budget_min": 5000000,
                "budget_max": 7500000,
                "preferred_city": "Bengaluru",
                "preferred_area": "Whitefield",
                "preferred_property_type": "APARTMENT",
                "preferred_bhk": "THREE_BHK",
                "customer_type": "BUYER",
                "customer_source": "WEBSITE",
                "status": "ACTIVE",
                "is_active": True,
                "lead_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "assigned_to_id": 12,
                "next_followup_date": "2026-08-01",
            }
        },
    )

    first_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Customer's given name (1-100 characters).",
        examples=["Rohan"],
    )
    last_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Customer's family name (1-100 characters).",
        examples=["Sharma"],
    )

    # NOTE: all other field/model validators (text normalization, KYC
    # formats, non-negativity, DOB, budget-range) are inherited from
    # `_CustomerValidatorMixin`. Only `phone` is redeclared here, since
    # its validation mode differs from `CustomerUpdate`'s (this field is
    # required/non-optional on `CustomerCreate`).
    _validate_phone_fmt = field_validator("phone", mode="after")(_validate_phone)


# --------------------------------------------------------------------------
# Customer Update Schema
# --------------------------------------------------------------------------
class CustomerUpdate(_CustomerValidatorMixin):
    """
    Request payload for partially updating an existing customer (PATCH
    semantics). Every field is optional; only supplied fields are
    intended to be applied by the service layer.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    middle_name: Optional[str] = Field(default=None, max_length=100)
    last_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    date_of_birth: Optional[date] = Field(default=None)
    gender: Optional[Gender] = Field(default=None)
    marital_status: Optional[MaritalStatus] = Field(default=None)
    nationality: Optional[str] = Field(default=None, max_length=100)

    email: Optional[EmailStr] = Field(default=None)
    phone: Optional[str] = Field(default=None, max_length=20)
    alternate_phone: Optional[str] = Field(default=None, max_length=20)

    occupation: Optional[str] = Field(default=None, max_length=150)
    company_name: Optional[str] = Field(default=None, max_length=200)
    annual_income: Optional[Decimal] = Field(default=None)

    pan_number: Optional[str] = Field(default=None, max_length=10)
    aadhaar_number: Optional[str] = Field(default=None, max_length=14)
    passport_number: Optional[str] = Field(default=None, max_length=20)

    address_line_1: Optional[str] = Field(default=None, max_length=255)
    address_line_2: Optional[str] = Field(default=None, max_length=255)
    landmark: Optional[str] = Field(default=None, max_length=150)
    city: Optional[str] = Field(default=None, max_length=100)
    state: Optional[str] = Field(default=None, max_length=100)
    country: Optional[str] = Field(default=None, max_length=100)
    postal_code: Optional[str] = Field(default=None, max_length=20)

    budget_min: Optional[Decimal] = Field(default=None)
    budget_max: Optional[Decimal] = Field(default=None)
    preferred_city: Optional[str] = Field(default=None, max_length=100)
    preferred_area: Optional[str] = Field(default=None, max_length=150)
    preferred_property_type: Optional[PreferredPropertyType] = Field(default=None)
    preferred_bhk: Optional[PreferredBHK] = Field(default=None)

    customer_type: Optional[CustomerType] = Field(default=None)
    customer_source: Optional[CustomerSource] = Field(default=None)
    status: Optional[CustomerStatus] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    is_active: Optional[bool] = Field(default=None)

    lead_id: Optional[uuid.UUID] = Field(default=None)
    assigned_to_id: Optional[int] = Field(default=None)

    next_followup_date: Optional[date] = Field(default=None)

    # NOTE: all other field/model validators (text normalization, KYC
    # formats, non-negativity, DOB, budget-range) are inherited from
    # `_CustomerValidatorMixin`. Only `phone` is redeclared here, since
    # its validation mode differs from `CustomerCreate`'s (this field is
    # optional on `CustomerUpdate`).
    _validate_phone_fmt = field_validator("phone", mode="before")(_validate_phone)

    @model_validator(mode="after")
    def _validate_not_empty(self) -> "CustomerUpdate":
        """Rejects a payload where every field was omitted (no-op update)."""
        if not self.model_fields_set:
            raise ValueError("At least one field must be supplied for an update.")
        return self


# --------------------------------------------------------------------------
# Customer Response Schema
# --------------------------------------------------------------------------
class CustomerResponse(CustomerBase):
    """
    Outward-facing representation of a customer, returned by customer
    retrieval and mutation endpoints. Extends `CustomerBase` with
    identity, audit, and engagement-tracking fields populated by the
    persistence layer. Never exposes ORM relationship objects
    (`lead`, `assigned_to`, `created_by`, `updated_by`) — only their
    scalar foreign-key IDs.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(
        ...,
        description="Globally unique identifier of the customer record.",
        examples=["3fa85f64-5717-4562-b3fc-2c963f66afa6"],
    )
    created_by_id: int = Field(
        ...,
        description="Internal ID of the User who created this customer record.",
        examples=[5],
    )
    updated_by_id: Optional[int] = Field(
        default=None,
        description="Internal ID of the User who last modified this customer record.",
        examples=[7],
    )
    last_contacted_at: Optional[datetime] = Field(
        default=None,
        description="UTC timestamp of the most recent contact with this customer.",
        examples=["2026-07-20T09:15:00Z"],
    )
    created_at: datetime = Field(
        ...,
        description="UTC timestamp when the customer record was created.",
        examples=["2026-07-01T10:30:00Z"],
    )
    updated_at: datetime = Field(
        ...,
        description="UTC timestamp when the customer record was last updated.",
        examples=["2026-07-05T14:15:00Z"],
    )


# --------------------------------------------------------------------------
# Customer List Response Schema
# --------------------------------------------------------------------------
class CustomerListResponse(BaseModel):
    """
    Paginated collection response for customer listing/search endpoints,
    mirroring `LeadListResponse`'s envelope shape.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[CustomerResponse] = Field(
        ...,
        description="The page of customer records matching the query.",
    )
    total: int = Field(
        ...,
        ge=0,
        description="Total number of customer records matching the query, across all pages.",
        examples=[142],
    )
    page: int = Field(
        ...,
        ge=1,
        description="Current page number (1-indexed).",
        examples=[1],
    )
    page_size: int = Field(
        ...,
        ge=1,
        description="Number of records requested per page.",
        examples=[20],
    )
    total_pages: int = Field(
        ...,
        ge=0,
        description="Total number of pages available for the given page_size.",
        examples=[8],
    )


# --------------------------------------------------------------------------
# Customer Search Filters Schema
# --------------------------------------------------------------------------
class CustomerSearchFilters(BaseModel):
    """
    Query parameters for searching and filtering customers, including
    pagination and sorting controls.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    search: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Free-text search term matched against name/phone/email/notes.",
        examples=["Rohan"],
    )
    status: Optional[CustomerStatus] = Field(default=None, description="Filter by lifecycle status.")
    customer_type: Optional[CustomerType] = Field(default=None, description="Filter by commercial role.")
    customer_source: Optional[CustomerSource] = Field(
        default=None, description="Filter by acquisition channel."
    )
    city: Optional[str] = Field(default=None, max_length=100, description="Filter by city.")
    preferred_city: Optional[str] = Field(
        default=None, max_length=100, description="Filter by preferred city."
    )
    lead_id: Optional[uuid.UUID] = Field(
        default=None, description="Filter by originating Lead record ID."
    )
    assigned_to_id: Optional[int] = Field(
        default=None, description="Filter by the assigned agent's internal User ID."
    )
    created_by_id: Optional[int] = Field(
        default=None, description="Filter by the creator's internal User ID."
    )
    updated_by_id: Optional[int] = Field(
        default=None, description="Filter by the last updater's internal User ID."
    )
    budget_min: Optional[Decimal] = Field(
        default=None, ge=0, description="Filter by minimum budget lower bound."
    )
    budget_max: Optional[Decimal] = Field(
        default=None, ge=0, description="Filter by maximum budget upper bound."
    )
    annual_income_min: Optional[Decimal] = Field(
        default=None, ge=0, description="Filter by minimum annual income lower bound."
    )
    annual_income_max: Optional[Decimal] = Field(
        default=None, ge=0, description="Filter by annual income upper bound."
    )
    date_of_birth: Optional[date] = Field(default=None, description="Filter by exact date of birth.")
    next_followup_date: Optional[date] = Field(
        default=None, description="Filter by exact next follow-up date."
    )
    created_from: Optional[datetime] = Field(
        default=None, description="Filter by earliest record-creation timestamp (inclusive)."
    )
    created_to: Optional[datetime] = Field(
        default=None, description="Filter by latest record-creation timestamp (inclusive)."
    )
    page: int = Field(default=1, ge=1, description="Page number to retrieve (1-indexed).")
    page_size: int = Field(
        default=20, ge=1, le=200, description="Number of records to return per page (max 200)."
    )
    sort_by: str = Field(
        default="created_at",
        description=(
            "Field to sort results by. Resolved exclusively against the "
            "repository layer's own column allow-list; any unrecognized "
            "value safely falls back to 'created_at' and never reaches "
            "a raw ORDER BY clause."
        ),
        examples=["created_at"],
    )
    sort_order: str = Field(default="desc", description="Sort direction: 'asc' or 'desc'.")

    @field_validator("search")
    @classmethod
    def _validate_search(cls, value: Optional[str]) -> Optional[str]:
        """Rejects a search term that is empty after whitespace stripping."""
        if value is not None and not value.strip():
            raise ValueError("Search term must not be empty or whitespace only.")
        return value

    # NOTE: sort_by is intentionally NOT re-validated against a whitelist
    # here. `CustomerRepository.SORTABLE_FIELDS` already resolves this
    # value against its own column allow-list (falling back safely to
    # `Customer.created_at` for any unrecognized key), so duplicating
    # that whitelist at the schema layer would just be two lists to keep
    # in sync. No raw string ever reaches an ORDER BY clause either way.

    @field_validator("sort_order")
    @classmethod
    def _validate_sort_order(cls, value: str) -> str:
        """Validates and normalizes `sort_order` to lowercase 'asc'/'desc'."""
        normalized = value.strip().lower()
        if normalized not in {"asc", "desc"}:
            raise ValueError("sort_order must be either 'asc' or 'desc'.")
        return normalized

    @model_validator(mode="after")
    def _validate_ranges(self) -> "CustomerSearchFilters":
        """Ensures min/max and from/to range filters are internally consistent."""
        if (
            self.budget_min is not None
            and self.budget_max is not None
            and self.budget_max < self.budget_min
        ):
            raise ValueError("budget_max must be greater than or equal to budget_min.")
        if (
            self.annual_income_min is not None
            and self.annual_income_max is not None
            and self.annual_income_max < self.annual_income_min
        ):
            raise ValueError("annual_income_max must be greater than or equal to annual_income_min.")
        if (
            self.created_from is not None
            and self.created_to is not None
            and self.created_to < self.created_from
        ):
            raise ValueError("created_to must be greater than or equal to created_from.")
        return self


# --------------------------------------------------------------------------
# Customer Export Request Schema
# --------------------------------------------------------------------------
class CustomerExportRequest(BaseModel):
    """
    Request payload for exporting a filtered customer list to CSV or
    XLSX.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    export_format: str = Field(
        default="csv",
        description="Desired export file format: 'csv' or 'xlsx'.",
        examples=["csv"],
    )
    filters: Optional[CustomerSearchFilters] = Field(
        default=None,
        description="Optional filters to scope which customer records are exported.",
    )
    fields: Optional[list[str]] = Field(
        default=None,
        description=(
            "Optional explicit list of Customer attribute names to include as "
            "export columns. If omitted, a default enterprise column set is used."
        ),
        examples=[["first_name", "last_name", "email", "phone", "status"]],
    )
    include_kyc_fields: bool = Field(
        default=False,
        description=(
            "Whether to append KYC columns (pan_number, aadhaar_number, "
            "passport_number) to the default export column set. Ignored when "
            "`fields` is explicitly supplied."
        ),
    )

    @field_validator("export_format")
    @classmethod
    def _validate_export_format(cls, value: str) -> str:
        """Validates that `export_format` is one of the supported file formats."""
        normalized = value.strip().lower()
        if normalized not in {"csv", "xlsx"}:
            raise ValueError("export_format must be either 'csv' or 'xlsx'.")
        return normalized


# --------------------------------------------------------------------------
# Customer Statistics Response Schema
# --------------------------------------------------------------------------
class CustomerStatisticsResponse(BaseModel):
    """
    Aggregate Customer analytics for reporting and AI-copilot insight
    generation, as returned by `GET /customers/statistics`.
    """

    model_config = ConfigDict(from_attributes=True)

    total_customers: int = Field(
        ...,
        ge=0,
        description="Total number of customer records within the queried period.",
        examples=[142],
    )
    customers_by_status: dict[str, int] = Field(
        ...,
        description="Customer counts grouped by lifecycle status.",
        examples=[{"ACTIVE": 90, "PROSPECT": 30, "INACTIVE": 22}],
    )
    customers_by_type: dict[str, int] = Field(
        ...,
        description="Customer counts grouped by commercial role.",
        examples=[{"BUYER": 80, "TENANT": 40, "INVESTOR": 22}],
    )
    customers_by_source: dict[str, int] = Field(
        ...,
        description="Customer counts grouped by acquisition channel.",
        examples=[{"WEBSITE": 60, "REFERRAL": 50, "OTHER": 32}],
    )
    customers_by_city: dict[str, int] = Field(
        ...,
        description="Customer counts grouped by city, limited to the top cities by volume.",
        examples=[{"Bengaluru": 55, "Mumbai": 30}],
    )
    average_annual_income: Optional[Decimal] = Field(
        default=None,
        description="Average declared annual income across the queried period, if any customers had one set.",
        examples=[1650000],
    )
    average_budget_max: Optional[Decimal] = Field(
        default=None,
        description="Average maximum budget across the queried period, if any customers had one set.",
        examples=[8200000],
    )
    conversion_rate_from_leads: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        description="Percentage of customers in the queried period that originated from a Lead.",
        examples=[38.5],
    )
    period_start: Optional[date] = Field(
        default=None,
        description="Inclusive lower bound on `created_at` used to scope these statistics, if supplied.",
        examples=["2026-01-01"],
    )
    period_end: Optional[date] = Field(
        default=None,
        description="Inclusive upper bound on `created_at` used to scope these statistics, if supplied.",
        examples=["2026-06-30"],
    )