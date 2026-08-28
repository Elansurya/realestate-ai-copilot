"""
backend/app/schemas/integration.py

Pydantic v2 schemas for the Integration Management module of the
Enterprise Real Estate AI Copilot CRM.

Mirrors the shape of `app/models/integration.py` and follows the same
naming/style conventions already established elsewhere in the project
(e.g. `app/schemas/task.py`, `app/schemas/search.py`):
    - `*Create` / `*Update` -> payloads accepted to create/mutate a
      record.
    - `*Response`           -> representation returned by the API.
    - `*Filter`              -> structured filter/criteria payload.
    - Reusable `IntegrationPaginationParams` / `IntegrationSortingParams`
      building blocks, embedded wherever pagination/sorting is
      accepted, mirroring the inline pagination/sort fields already
      used by `app/schemas/task.py`'s `TaskFilter` and
      `app/schemas/search.py`'s `SearchPaginationParams` /
      `SearchSortingParams`.

Security note: `IntegrationCreate.credentials` / `IntegrationUpdate.credentials`
accept only a raw, in-transit secrets payload (e.g. an API key, OAuth
tokens, SMTP password) validated as an opaque JSON object at this
layer. Encryption-at-rest, and ensuring that `IntegrationResponse`
never echoes credentials back to a caller, are business-logic/service
and response-model concerns respectively -- `IntegrationResponse`
deliberately has NO `credentials` field so a secret can never be
serialized back out through this schema.

These schemas define the request/response contracts for creating,
updating, listing, and health-checking integrations. They are
consumed by the (separately implemented, out of scope for this file)
repository/service/router layers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.models.integration import (
    AuthenticationType,
    IntegrationProvider,
    IntegrationStatus,
    IntegrationType,
)

__all__ = [
    "IntegrationPaginationParams",
    "IntegrationSortingParams",
    "IntegrationFilter",
    "IntegrationCreate",
    "IntegrationUpdate",
    "IntegrationStatusUpdate",
    "IntegrationHealthCheck",
    "IntegrationResponse",
    "IntegrationListResponse",
    "IntegrationStatisticsResponse",
]

#: Columns callers may sort integration listings by.
_ALLOWED_SORT_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "provider",
        "integration_type",
        "status",
        "created_at",
        "updated_at",
        "last_sync_at",
        "last_health_check_at",
        "is_default",
    }
)

#: A given `IntegrationProvider` is only ever valid for a subset of
#: `IntegrationType` values; this allow-list backs the cross-field
#: validation performed by `IntegrationCreate`/`IntegrationUpdate`.
_PROVIDER_TYPE_MAP: dict[IntegrationProvider, IntegrationType] = {
    IntegrationProvider.SMTP: IntegrationType.EMAIL,
    IntegrationProvider.SMS_PROVIDER: IntegrationType.SMS,
    IntegrationProvider.WHATSAPP_BUSINESS: IntegrationType.WHATSAPP,
    IntegrationProvider.GOOGLE_CALENDAR: IntegrationType.CALENDAR,
    IntegrationProvider.GOOGLE_DRIVE: IntegrationType.STORAGE,
    IntegrationProvider.AWS_S3: IntegrationType.STORAGE,
    IntegrationProvider.AZURE_BLOB_STORAGE: IntegrationType.STORAGE,
    IntegrationProvider.FIREBASE: IntegrationType.NOTIFICATION,
    IntegrationProvider.OPENAI: IntegrationType.AI_PROVIDER,
    IntegrationProvider.ANTHROPIC: IntegrationType.AI_PROVIDER,
    IntegrationProvider.GEMINI: IntegrationType.AI_PROVIDER,
    IntegrationProvider.HUGGING_FACE: IntegrationType.AI_PROVIDER,
    IntegrationProvider.RAZORPAY: IntegrationType.PAYMENT_GATEWAY,
    IntegrationProvider.STRIPE: IntegrationType.PAYMENT_GATEWAY,
    IntegrationProvider.WEBHOOK_TARGET: IntegrationType.WEBHOOK,
    IntegrationProvider.CUSTOM_REST_API: IntegrationType.CUSTOM_API,
}

MIN_NAME_LENGTH: int = 2
MAX_NAME_LENGTH: int = 150
MIN_TIMEOUT_SECONDS: int = 1
MAX_TIMEOUT_SECONDS: int = 300
MIN_RETRY_COUNT: int = 0
MAX_RETRY_COUNT: int = 10


# ---------------------------------------------------------------------------
# Reusable pagination / sorting building blocks
# ---------------------------------------------------------------------------
class IntegrationPaginationParams(BaseModel):
    """Reusable pagination parameters shared by integration-related requests.

    Attributes:
        page: 1-indexed page number to retrieve.
        page_size: Number of items to retrieve per page.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    page: int = Field(default=1, ge=1, description="1-indexed page number.")
    page_size: int = Field(
        default=20, ge=1, le=200, description="Number of items per page."
    )


class IntegrationSortingParams(BaseModel):
    """Reusable sort parameters shared by integration-related requests.

    Attributes:
        sort_by: Column/field name to sort results by.
        sort_order: Sort direction, either ``"asc"`` or ``"desc"``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    _ALLOWED_SORT_FIELDS: ClassVar[frozenset] = _ALLOWED_SORT_FIELDS

    sort_by: str = Field(
        default="created_at", description="Field name to sort results by."
    )
    sort_order: str = Field(
        default="desc", description="Sort direction: 'asc' or 'desc'."
    )

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
        if value not in _ALLOWED_SORT_FIELDS:
            raise ValueError(
                f"sort_by must be one of: {sorted(_ALLOWED_SORT_FIELDS)}"
            )
        return value

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


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------
class IntegrationFilter(BaseModel):
    """Schema encapsulating structured filter criteria for listing integrations.

    Attributes:
        integration_type: Restrict to a specific integration type.
        provider: Restrict to a specific provider.
        status: Restrict to a specific status.
        authentication_type: Restrict to a specific authentication type.
        is_default: Restrict to default (or non-default) integrations only.
        search: Free-text match against `name`.
        created_from: Inclusive lower bound on `created_at`.
        created_to: Inclusive upper bound on `created_at`.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    integration_type: Optional[IntegrationType] = Field(
        default=None, description="Restrict to a specific integration type."
    )
    provider: Optional[IntegrationProvider] = Field(
        default=None, description="Restrict to a specific provider."
    )
    status: Optional[IntegrationStatus] = Field(
        default=None, description="Restrict to a specific status."
    )
    authentication_type: Optional[AuthenticationType] = Field(
        default=None, description="Restrict to a specific authentication type."
    )
    is_default: Optional[bool] = Field(
        default=None, description="Restrict to default/non-default integrations."
    )
    search: Optional[str] = Field(
        default=None,
        max_length=150,
        description="Free-text match against the integration name.",
    )
    created_from: Optional[datetime] = Field(
        default=None, description="Inclusive lower bound on created_at."
    )
    created_to: Optional[datetime] = Field(
        default=None, description="Inclusive upper bound on created_at."
    )

    @model_validator(mode="after")
    def _validate_date_range(self) -> "IntegrationFilter":
        """Ensures the provided creation-date range is chronologically valid.

        Returns:
            IntegrationFilter: The validated model instance.

        Raises:
            ValueError: If `created_from` is after `created_to`.
        """
        if (
            self.created_from
            and self.created_to
            and self.created_from > self.created_to
        ):
            raise ValueError("created_from must not be after created_to.")
        return self


# ---------------------------------------------------------------------------
# Create / Update
# ---------------------------------------------------------------------------
class IntegrationCreate(BaseModel):
    """Schema used to create a new Integration.

    Attributes:
        name: Human-readable, unique name for this integration instance.
        provider: The specific third-party provider.
        integration_type: The functional category of the integration.
        authentication_type: The authentication mechanism to use.
        configuration: Non-secret, provider-specific settings.
        credentials: Raw (pre-encryption) credentials payload; the
            service layer is responsible for encrypting this before
            persistence -- see module docstring.
        base_url: Base URL of the external API/service, if applicable.
        api_version: API version string/identifier, if applicable.
        webhook_url: Associated webhook URL, if applicable.
        timeout_seconds: Per-request timeout, in seconds.
        retry_count: Number of retries on a failed request.
        rate_limit_per_minute: Maximum requests per minute, if enforced.
        is_default: Whether this should become the default integration
            for its `integration_type`.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        ...,
        min_length=MIN_NAME_LENGTH,
        max_length=MAX_NAME_LENGTH,
        description="Human-readable, unique integration name.",
    )
    provider: IntegrationProvider = Field(..., description="The specific provider.")
    integration_type: IntegrationType = Field(
        ..., description="The functional category of the integration."
    )
    authentication_type: AuthenticationType = Field(
        default=AuthenticationType.API_KEY,
        description="The authentication mechanism to use.",
    )
    configuration: Optional[dict[str, Any]] = Field(
        default=None, description="Non-secret, provider-specific settings."
    )
    credentials: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Raw credentials payload; encrypted at rest by the service "
            "layer prior to persistence. Never echoed back in responses."
        ),
    )
    base_url: Optional[str] = Field(
        default=None, max_length=500, description="Base URL of the external service."
    )
    api_version: Optional[str] = Field(
        default=None, max_length=50, description="API version identifier."
    )
    webhook_url: Optional[str] = Field(
        default=None, max_length=500, description="Associated webhook URL."
    )
    timeout_seconds: int = Field(
        default=30,
        ge=MIN_TIMEOUT_SECONDS,
        le=MAX_TIMEOUT_SECONDS,
        description="Per-request timeout, in seconds.",
    )
    retry_count: int = Field(
        default=3,
        ge=MIN_RETRY_COUNT,
        le=MAX_RETRY_COUNT,
        description="Number of retries on a failed request.",
    )
    rate_limit_per_minute: Optional[int] = Field(
        default=None, gt=0, description="Maximum requests per minute, if enforced."
    )
    is_default: bool = Field(
        default=False,
        description="Whether this becomes the default integration for its type.",
    )

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        """Ensures the name is not blank after stripping whitespace.

        Args:
            value: The raw name text.

        Returns:
            str: The validated, stripped name.

        Raises:
            ValueError: If the stripped value is empty.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank.")
        return stripped

    @field_validator("base_url", "webhook_url")
    @classmethod
    def _validate_url_scheme(cls, value: Optional[str]) -> Optional[str]:
        """Ensures a supplied URL uses the http/https scheme.

        Args:
            value: The raw URL string, if supplied.

        Returns:
            Optional[str]: The validated URL, or `None`.

        Raises:
            ValueError: If the URL does not start with `http://` or `https://`.
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if not (stripped.startswith("http://") or stripped.startswith("https://")):
            raise ValueError("URL must start with 'http://' or 'https://'.")
        return stripped

    @model_validator(mode="after")
    def _validate_provider_type_consistency(self) -> "IntegrationCreate":
        """Ensures `provider` and `integration_type` are a recognized pairing.

        Returns:
            IntegrationCreate: The validated model instance.

        Raises:
            ValueError: If `provider` is not compatible with
                `integration_type` per the project's supported
                provider/type mapping.
        """
        expected_type = _PROVIDER_TYPE_MAP.get(self.provider)
        if expected_type is not None and expected_type != self.integration_type:
            raise ValueError(
                f"provider '{self.provider.value}' is not valid for "
                f"integration_type '{self.integration_type.value}'; expected "
                f"'{expected_type.value}'."
            )
        return self

    @model_validator(mode="after")
    def _validate_auth_requires_credentials(self) -> "IntegrationCreate":
        """Ensures a non-`NONE` authentication type is given credentials.

        Returns:
            IntegrationCreate: The validated model instance.

        Raises:
            ValueError: If `authentication_type` requires credentials
                but none were supplied.
        """
        if (
            self.authentication_type != AuthenticationType.NONE
            and not self.credentials
        ):
            raise ValueError(
                f"credentials are required for authentication_type "
                f"'{self.authentication_type.value}'."
            )
        return self


class IntegrationUpdate(BaseModel):
    """Schema used to update an existing Integration.

    All fields are optional; only supplied fields should be applied by
    the service layer (partial update semantics).

    Attributes:
        name: Updated human-readable name.
        authentication_type: Updated authentication mechanism.
        configuration: Updated non-secret, provider-specific settings
            (replaces the existing value wholesale).
        credentials: Updated raw credentials payload, re-encrypted by
            the service layer; omit to leave existing credentials
            unchanged.
        base_url: Updated base URL.
        api_version: Updated API version identifier.
        webhook_url: Updated webhook URL.
        timeout_seconds: Updated per-request timeout, in seconds.
        retry_count: Updated retry count.
        rate_limit_per_minute: Updated rate limit, or `None` to clear it.
        is_default: Updated default-integration flag.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: Optional[str] = Field(
        default=None, min_length=MIN_NAME_LENGTH, max_length=MAX_NAME_LENGTH
    )
    authentication_type: Optional[AuthenticationType] = None
    configuration: Optional[dict[str, Any]] = None
    credentials: Optional[dict[str, Any]] = None
    base_url: Optional[str] = Field(default=None, max_length=500)
    api_version: Optional[str] = Field(default=None, max_length=50)
    webhook_url: Optional[str] = Field(default=None, max_length=500)
    timeout_seconds: Optional[int] = Field(
        default=None, ge=MIN_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS
    )
    retry_count: Optional[int] = Field(
        default=None, ge=MIN_RETRY_COUNT, le=MAX_RETRY_COUNT
    )
    rate_limit_per_minute: Optional[int] = Field(default=None, gt=0)
    is_default: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: Optional[str]) -> Optional[str]:
        """Ensures a supplied name is not blank after stripping whitespace.

        Args:
            value: The raw name text, if supplied.

        Returns:
            Optional[str]: The validated, stripped name, or `None`.

        Raises:
            ValueError: If a value was supplied but is blank.
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank.")
        return stripped

    @field_validator("base_url", "webhook_url")
    @classmethod
    def _validate_url_scheme(cls, value: Optional[str]) -> Optional[str]:
        """Ensures a supplied URL uses the http/https scheme.

        Args:
            value: The raw URL string, if supplied.

        Returns:
            Optional[str]: The validated URL, or `None`.

        Raises:
            ValueError: If the URL does not start with `http://` or `https://`.
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if not (stripped.startswith("http://") or stripped.startswith("https://")):
            raise ValueError("URL must start with 'http://' or 'https://'.")
        return stripped

    @model_validator(mode="after")
    def _validate_auth_requires_credentials(self) -> "IntegrationUpdate":
        """Ensures switching to a non-`NONE` auth type is paired with credentials.

        Only enforced when `authentication_type` is explicitly being
        changed in this update; a caller updating unrelated fields on
        an integration that already has credentials on file is not
        required to resupply them.

        Returns:
            IntegrationUpdate: The validated model instance.

        Raises:
            ValueError: If `authentication_type` is being changed to a
                value other than `NONE` in the same request that
                explicitly clears `credentials`.
        """
        if (
            self.authentication_type is not None
            and self.authentication_type != AuthenticationType.NONE
            and "credentials" in self.model_fields_set
            and not self.credentials
        ):
            raise ValueError(
                f"credentials are required when setting authentication_type "
                f"to '{self.authentication_type.value}'."
            )
        return self


class IntegrationStatusUpdate(BaseModel):
    """Schema used to transition an Integration's operational status.

    Attributes:
        status: The new status to apply.
        reason: Optional free-text reason for the transition (e.g. why
            an integration is being disabled), for audit purposes.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    status: IntegrationStatus = Field(..., description="The new status to apply.")
    reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Optional reason for the status transition, for audit purposes.",
    )


class IntegrationHealthCheck(BaseModel):
    """Schema representing the outcome of an Integration health check.

    Attributes:
        integration_id: Identifier of the integration that was checked.
        is_healthy: Whether the health check succeeded.
        status: The resulting status to apply following this check.
        checked_at: Timestamp the health check was performed.
        latency_ms: Observed round-trip latency of the check, in
            milliseconds, if measured.
        message: Optional human-readable detail (e.g. an error message
            on failure).
    """

    model_config = ConfigDict(from_attributes=True)

    integration_id: uuid.UUID
    is_healthy: bool
    status: IntegrationStatus
    checked_at: datetime
    latency_ms: Optional[float] = Field(default=None, ge=0)
    message: Optional[str] = Field(default=None, max_length=1000)


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------
class IntegrationResponse(BaseModel):
    """Schema representing a persisted `Integration` record.

    Note: this schema intentionally has NO `credentials` field, so
    encrypted/raw secret material can never be serialized back to a
    caller through this response model.

    Attributes:
        id: Surrogate primary key of the integration.
        name: Human-readable name for this integration instance.
        provider: The specific third-party provider.
        integration_type: The functional category of the integration.
        status: Current operational status.
        authentication_type: The authentication mechanism used.
        configuration: Non-secret, provider-specific settings.
        base_url: Base URL of the external API/service, if applicable.
        api_version: API version string/identifier, if applicable.
        webhook_url: Associated webhook URL, if applicable.
        timeout_seconds: Per-request timeout, in seconds.
        retry_count: Number of retries on a failed request.
        rate_limit_per_minute: Maximum requests per minute, if enforced.
        is_default: Whether this is the default integration for its type.
        last_sync_at: Timestamp of the last successful sync, if any.
        last_health_check_at: Timestamp of the last health check, if any.
        created_by: Identifier of the user who created this integration.
        created_at: Record creation timestamp.
        updated_at: Record last-update timestamp.
        has_credentials: Derived flag indicating whether credentials
            are on file, without ever exposing their content.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    provider: IntegrationProvider
    integration_type: IntegrationType
    status: IntegrationStatus
    authentication_type: AuthenticationType
    configuration: Optional[dict[str, Any]] = None
    base_url: Optional[str] = None
    api_version: Optional[str] = None
    webhook_url: Optional[str] = None
    timeout_seconds: int
    retry_count: int
    rate_limit_per_minute: Optional[int] = None
    is_default: bool = False
    last_sync_at: Optional[datetime] = None
    last_health_check_at: Optional[datetime] = None
    created_by: Optional[int] = Field(default=None, validation_alias="created_by_id")
    created_at: datetime
    updated_at: datetime
    has_credentials: bool = Field(
        default=False,
        description=(
            "Whether credentials are on file, without exposing them. "
            "Not derivable via `from_attributes` alone (the ORM model "
            "has no such attribute); the service layer is responsible "
            "for setting this explicitly, e.g. via "
            "`IntegrationResponse.model_validate(record, update={"
            "'has_credentials': record.credentials is not None})` or "
            "equivalent, when constructing this response from an "
            "`Integration` ORM instance."
        ),
    )


class IntegrationListResponse(BaseModel):
    """Schema representing a paginated collection of Integration records.

    Attributes:
        items: The integration records for the current page.
        total: Total number of records matching the query, across all pages.
        page: Current page number (1-indexed).
        page_size: Number of items requested per page.
        total_pages: Total number of pages available.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[IntegrationResponse] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _compute_total_pages_if_needed(self) -> "IntegrationListResponse":
        """Recomputes ``total_pages`` defensively when it appears inconsistent.

        Returns:
            IntegrationListResponse: The validated model instance.
        """
        expected_pages = (
            (self.total + self.page_size - 1) // self.page_size
            if self.page_size
            else 0
        )
        if self.total_pages != expected_pages:
            self.total_pages = expected_pages
        return self


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
class IntegrationStatisticsResponse(BaseModel):
    """Schema representing aggregate statistics over a set of integrations.

    Attributes:
        total_integrations: Total number of integrations in scope.
        by_type: Count of integrations grouped by `integration_type`.
        by_provider: Count of integrations grouped by `provider`.
        by_status: Count of integrations grouped by `status`.
        by_authentication_type: Count of integrations grouped by
            `authentication_type`.
        active_count: Number of integrations with status `active`.
        failed_count: Number of integrations with status `failed`.
        default_count: Number of integrations flagged `is_default`.
        last_sync_at: Most recent `last_sync_at` observed across
            integrations in scope, if any have synced.
        last_health_check_at: Most recent `last_health_check_at`
            observed across integrations in scope, if any have been
            checked.
    """

    model_config = ConfigDict(from_attributes=True)

    total_integrations: int = Field(..., ge=0)
    by_type: dict[str, int] = Field(default_factory=dict)
    by_provider: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    by_authentication_type: dict[str, int] = Field(default_factory=dict)
    active_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    default_count: int = Field(default=0, ge=0)
    last_sync_at: Optional[datetime] = None
    last_health_check_at: Optional[datetime] = None