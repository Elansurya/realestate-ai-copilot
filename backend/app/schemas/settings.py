"""Pydantic v2 schemas for the Settings module.

These schemas define the request/response contracts for creating,
updating, filtering, listing, and summarizing system configuration
entries. They are consumed by the (separately implemented) service and
router layers.
"""

import uuid
from datetime import datetime
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.settings import SettingCategory, SettingDataType

__all__ = [
    "SettingsCreate",
    "SettingsUpdate",
    "SettingsResponse",
    "SettingsListResponse",
    "SettingsFilter",
    "SettingsStatisticsResponse",
]


class SettingsBase(BaseModel):
    """Shared base fields common to setting creation and representation.

    Attributes:
        category: Functional area this setting belongs to.
        setting_key: Unique-per-category configuration key.
        setting_value: The configured value.
        description: Human-readable explanation of what this setting controls.
        data_type: Logical data type of ``setting_value``.
        is_public: Whether this setting may be exposed to unauthenticated clients.
        is_editable: Whether this setting may be modified via the Settings UI/API.
        is_encrypted: Whether ``setting_value`` is stored in encrypted form.
        validation_rules: Optional ruleset used to validate ``setting_value``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    category: SettingCategory = Field(
        ..., description="Functional area this setting belongs to."
    )
    setting_key: str = Field(
        ...,
        min_length=1,
        max_length=150,
        description="Unique-per-category configuration key, e.g. 'SMTP_HOST'.",
    )
    setting_value: Optional[Any] = Field(
        default=None, description="The configured value, stored as JSONB."
    )
    description: Optional[str] = Field(
        default=None, description="Human-readable explanation of this setting."
    )
    data_type: SettingDataType = Field(
        default=SettingDataType.STRING,
        description="Logical data type of setting_value.",
    )
    is_public: bool = Field(
        default=False,
        description="Whether this setting may be exposed to unauthenticated clients.",
    )
    is_editable: bool = Field(
        default=True,
        description="Whether this setting may be modified via the Settings UI/API.",
    )
    is_encrypted: bool = Field(
        default=False,
        description="Whether setting_value is stored/transmitted encrypted.",
    )
    validation_rules: Optional[dict[str, Any]] = Field(
        default=None,
        description="Optional ruleset (min/max, regex, allowed values, etc.).",
    )

    @field_validator("setting_key")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """Ensures the setting key is not blank after stripping whitespace.

        Args:
            value: The raw field value.

        Returns:
            str: The validated, stripped value.

        Raises:
            ValueError: If the stripped value is empty.
        """
        if not value or not value.strip():
            raise ValueError("Field must not be empty or whitespace only.")
        return value

    @model_validator(mode="after")
    def _validate_encrypted_not_public(self) -> "SettingsBase":
        """Ensures an encrypted setting is never simultaneously public.

        Returns:
            SettingsBase: The validated model instance.

        Raises:
            ValueError: If ``is_encrypted`` and ``is_public`` are both true.
        """
        if self.is_encrypted and self.is_public:
            raise ValueError("A setting cannot be both is_encrypted and is_public.")
        return self


class SettingsCreate(SettingsBase):
    """Schema used to create a new setting entry.

    Attributes:
        created_by: Identifier of the user creating the setting, if any.
    """

    created_by: Optional[int] = Field(
        default=None, description="Identifier of the user creating the setting."
    )


class SettingsUpdate(BaseModel):
    """Schema used to partially update an existing setting entry.

    All fields are optional; only supplied fields are applied. ``category``
    and ``setting_key`` are intentionally omitted -- once created, a
    setting's identity (its category/key pair) is immutable, and callers
    should create a new setting rather than repoint an existing one.

    Attributes:
        setting_value: The new configured value.
        description: Updated human-readable explanation.
        data_type: Updated logical data type of ``setting_value``.
        is_public: Updated public-exposure flag.
        is_editable: Updated editability flag.
        is_encrypted: Updated encryption flag.
        validation_rules: Updated validation ruleset.
        updated_by: Identifier of the user performing the update.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    setting_value: Optional[Any] = None
    description: Optional[str] = None
    data_type: Optional[SettingDataType] = None
    is_public: Optional[bool] = None
    is_editable: Optional[bool] = None
    is_encrypted: Optional[bool] = None
    validation_rules: Optional[dict[str, Any]] = None
    updated_by: Optional[int] = Field(
        default=None, description="Identifier of the user performing the update."
    )



class SettingsResponse(SettingsBase):
    """Schema representing a persisted setting entry returned to clients.

    Attributes:
        id: Surrogate primary key of the setting.
        created_by: Identifier of the user who created the setting, if any.
        updated_by: Identifier of the user who last updated the setting, if any.
        created_at: Timestamp the entry was created.
        updated_at: Timestamp the entry was last updated.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class SettingsListResponse(BaseModel):
    """Schema representing a paginated collection of setting entries.

    Attributes:
        items: The setting entries for the current page.
        total: Total number of entries matching the query, across all pages.
        page: Current page number (1-indexed).
        page_size: Number of items requested per page.
        total_pages: Total number of pages available.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[SettingsResponse] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _compute_total_pages_if_needed(self) -> "SettingsListResponse":
        """Recomputes ``total_pages`` defensively when it appears inconsistent.

        Returns:
            SettingsListResponse: The validated model instance.
        """
        expected_pages = (
            (self.total + self.page_size - 1) // self.page_size
            if self.page_size
            else 0
        )
        if self.total_pages != expected_pages:
            self.total_pages = expected_pages
        return self


class SettingsFilter(BaseModel):
    """Schema encapsulating filter, sort, and pagination parameters for queries.

    Attributes:
        category: Restrict results to a specific category.
        setting_key: Restrict results to a specific setting key.
        data_type: Restrict results to a specific data type.
        is_public: Restrict results to a specific public-exposure state.
        is_editable: Restrict results to a specific editability state.
        is_encrypted: Restrict results to a specific encryption state.
        search: Free-text search applied to setting_key/description.
        date_from: Lower bound (inclusive) on ``created_at``.
        date_to: Upper bound (inclusive) on ``created_at``.
        page: Page number to retrieve (1-indexed).
        page_size: Number of items to retrieve per page.
        sort_by: Column name to sort by.
        sort_order: Sort direction, either ``"asc"`` or ``"desc"``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    category: Optional[SettingCategory] = None
    setting_key: Optional[str] = Field(default=None, max_length=150)
    data_type: Optional[SettingDataType] = None
    is_public: Optional[bool] = None
    is_editable: Optional[bool] = None
    is_encrypted: Optional[bool] = None
    search: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Free-text search on setting_key and description.",
    )
    date_from: Optional[datetime] = Field(
        default=None, description="Inclusive lower bound on created_at."
    )
    date_to: Optional[datetime] = Field(
        default=None, description="Inclusive upper bound on created_at."
    )

    page: int = Field(default=1, ge=1, description="1-indexed page number.")
    page_size: int = Field(
        default=20, ge=1, le=200, description="Number of items per page."
    )

    sort_by: str = Field(
        default="created_at", description="Column name to sort results by."
    )
    sort_order: str = Field(
        default="desc", description="Sort direction: 'asc' or 'desc'."
    )

    _ALLOWED_SORT_FIELDS: ClassVar[frozenset] = frozenset(
        {
            "created_at",
            "updated_at",
            "category",
            "setting_key",
            "data_type",
            "is_public",
            "is_editable",
        }
    )

    @field_validator("sort_order")
    @classmethod
    def _validate_sort_order(cls, value: str) -> str:
        """Validates that the sort order is one of the supported directions.

        Args:
            value: The requested sort order.

        Returns:
            str: The normalized (lowercased) sort order.

        Raises:
            ValueError: If the value is not ``"asc"`` or ``"desc"``.
        """
        normalized = value.strip().lower()
        if normalized not in {"asc", "desc"}:
            raise ValueError("sort_order must be either 'asc' or 'desc'.")
        return normalized

    @field_validator("sort_by")
    @classmethod
    def _validate_sort_by(cls, value: str) -> str:
        """Validates that the sort field is an allowed, indexed column.

        Args:
            value: The requested sort column name.

        Returns:
            str: The validated sort column name.

        Raises:
            ValueError: If the column is not in the allow-list.
        """
        if value not in cls._ALLOWED_SORT_FIELDS:
            raise ValueError(
                f"sort_by must be one of: {sorted(cls._ALLOWED_SORT_FIELDS)}"
            )
        return value

    @model_validator(mode="after")
    def _validate_date_range(self) -> "SettingsFilter":
        """Ensures the provided date range is chronologically valid.

        Returns:
            SettingsFilter: The validated model instance.

        Raises:
            ValueError: If ``date_from`` is after ``date_to``.
        """
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to.")
        return self


class SettingsStatisticsResponse(BaseModel):
    """Schema representing aggregate statistics over a set of setting entries.

    Attributes:
        total_settings: Total number of setting entries in scope.
        public_count: Number of entries with ``is_public`` true.
        editable_count: Number of entries with ``is_editable`` true.
        encrypted_count: Number of entries with ``is_encrypted`` true.
        by_category: Count of entries grouped by category.
        by_data_type: Count of entries grouped by data type.
        date_from: Inclusive lower bound of the statistics window, if scoped.
        date_to: Inclusive upper bound of the statistics window, if scoped.
    """

    model_config = ConfigDict(from_attributes=True)

    total_settings: int = Field(..., ge=0)
    public_count: int = Field(..., ge=0)
    editable_count: int = Field(..., ge=0)
    encrypted_count: int = Field(..., ge=0)
    by_category: dict[str, int] = Field(default_factory=dict)
    by_data_type: dict[str, int] = Field(default_factory=dict)
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None