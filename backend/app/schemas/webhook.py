"""
backend/app/schemas/webhook.py

Pydantic v2 schemas for the Enterprise Webhook module of the Enterprise
Real Estate AI Copilot CRM.

Mirrors the shape of `app/models/webhook.py` and follows the same
naming/style conventions already established elsewhere in the project
(e.g. `app/schemas/integration.py`, `app/schemas/monitoring.py`):
    - `*Create` / `*Update` -> payloads accepted to create/mutate a
      record.
    - `*Response`           -> representation returned by the API.
    - `*ListResponse`       -> paginated collection wrapper.
    - `*Filter`              -> structured filter/criteria + pagination
      + sorting payload, mirroring `app/schemas/monitoring.py`'s
      `HealthFilter` (a single combined filter/pagination/sort schema)
      rather than `app/schemas/integration.py`'s separate
      `*PaginationParams` / `*SortingParams` building blocks, since the
      Webhook Delivery module's list/log endpoints are expected to
      share one query-parameter surface per resource.

Security note: `WebhookCreate.secret_key` / `WebhookUpdate.secret_key`
accept only a raw, in-transit secret value (used for HMAC signing,
bearer/API-key, or basic-auth credentials depending on
`authentication_type`). Encryption-at-rest (if required) is a
service-layer concern. `WebhookResponse` deliberately has NO
`secret_key` field so a secret can never be serialized back out
through this schema -- it instead exposes a derived `has_secret_key`
boolean, mirroring `IntegrationResponse.has_credentials`.

These schemas define the request/response contracts for registering,
updating, listing, and reviewing delivery logs/statistics for
webhooks. They are consumed by the (separately implemented, out of
scope for this file) repository/service/router layers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.models.webhook import (
    AuthenticationType,
    DeliveryStatus,
    WebhookEvent,
    WebhookStatus,
)

__all__ = [
    "WebhookCreate",
    "WebhookUpdate",
    "WebhookResponse",
    "WebhookListResponse",
    "WebhookFilter",
    "WebhookLogResponse",
    "WebhookLogListResponse",
    "WebhookLogFilter",
    "WebhookStatisticsResponse",
]

# ---------------------------------------------------------------------------
# Shared Validation Constants
# ---------------------------------------------------------------------------
MIN_NAME_LENGTH: int = 2
MAX_NAME_LENGTH: int = 150
MAX_TARGET_URL_LENGTH: int = 2048
MAX_SECRET_KEY_LENGTH: int = 255

MIN_TIMEOUT_SECONDS: int = 1
MAX_TIMEOUT_SECONDS: int = 300
MIN_RETRY_COUNT: int = 0
MAX_RETRY_COUNT: int = 10

_ALLOWED_HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE"}
)

#: Columns callers may sort webhook listings by. Mirrors the indexed /
#: filterable columns declared on `Webhook` in `app/models/webhook.py`.
_WEBHOOK_ALLOWED_SORT_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "event",
        "status",
        "authentication_type",
        "enabled",
        "retry_count",
        "timeout_seconds",
        "last_delivery_at",
        "last_success_at",
        "last_failure_at",
        "created_at",
        "updated_at",
    }
)

#: Columns callers may sort webhook delivery log listings by. Mirrors
#: the indexed columns declared on `WebhookLog`.
_WEBHOOK_LOG_ALLOWED_SORT_FIELDS: frozenset[str] = frozenset(
    {
        "delivery_status",
        "attempt_count",
        "response_code",
        "duration_ms",
        "delivered_at",
        "created_at",
    }
)


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    """Strips surrounding whitespace and converts a blank string to `None`.

    Args:
        value: The raw string, or `None`.

    Returns:
        Optional[str]: The stripped string, or `None` if it was blank
        or already `None`.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _validate_url_scheme(value: str) -> str:
    """Validates that a URL uses the http/https scheme.

    Args:
        value: The raw URL string.

    Returns:
        str: The validated, stripped URL.

    Raises:
        ValueError: If the URL does not start with `http://` or `https://`.
    """
    stripped = value.strip()
    if not (stripped.startswith("http://") or stripped.startswith("https://")):
        raise ValueError("target_url must start with 'http://' or 'https://'.")
    return stripped


# ---------------------------------------------------------------------------
# Webhook Create Schema
# ---------------------------------------------------------------------------
class WebhookCreate(BaseModel):
    """Schema used to register a new outbound Webhook.

    Attributes:
        name: Human-readable, unique name for this webhook subscription.
        event: The domain event this webhook fires on.
        target_url: Destination URL events are delivered to.
        http_method: HTTP method used for delivery.
        authentication_type: Authentication/signing mechanism to use.
        secret_key: Raw secret used for HMAC signing or as a
            bearer/API-key/basic-auth credential; required unless
            `authentication_type` is `NONE`.
        custom_headers: Additional static HTTP headers sent with every
            delivery attempt.
        payload_template: Optional JSON template describing how the
            outbound request body should be shaped from the source
            event payload.
        retry_count: Maximum number of delivery retries on failure.
        timeout_seconds: Per-request delivery timeout, in seconds.
        rate_limit_per_minute: Maximum delivery attempts per minute,
            if enforced.
        enabled: Quick on/off toggle for delivery eligibility.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        ...,
        min_length=MIN_NAME_LENGTH,
        max_length=MAX_NAME_LENGTH,
        description="Human-readable, unique webhook name.",
    )
    event: WebhookEvent = Field(
        ..., description="The domain event this webhook fires on."
    )
    target_url: str = Field(
        ...,
        max_length=MAX_TARGET_URL_LENGTH,
        description="Destination URL events are delivered to.",
    )
    http_method: str = Field(
        default="POST", description="HTTP method used for delivery."
    )
    authentication_type: AuthenticationType = Field(
        default=AuthenticationType.HMAC_SIGNATURE,
        description="Authentication/signing mechanism to use.",
    )
    secret_key: Optional[str] = Field(
        default=None,
        max_length=MAX_SECRET_KEY_LENGTH,
        description=(
            "Raw secret used for HMAC signing or as a "
            "bearer/API-key/basic-auth credential. Never echoed back "
            "in responses."
        ),
    )
    custom_headers: Optional[dict[str, str]] = Field(
        default=None,
        description="Additional static HTTP headers sent with every delivery attempt.",
    )
    payload_template: Optional[dict[str, Any]] = Field(
        default=None,
        description="Optional template describing how the outbound body is shaped.",
    )
    retry_count: int = Field(
        default=3,
        ge=MIN_RETRY_COUNT,
        le=MAX_RETRY_COUNT,
        description="Maximum number of delivery retries on failure.",
    )
    timeout_seconds: int = Field(
        default=30,
        ge=MIN_TIMEOUT_SECONDS,
        le=MAX_TIMEOUT_SECONDS,
        description="Per-request delivery timeout, in seconds.",
    )
    rate_limit_per_minute: Optional[int] = Field(
        default=None, gt=0, description="Maximum delivery attempts per minute, if enforced."
    )
    enabled: bool = Field(
        default=True, description="Quick on/off toggle for delivery eligibility."
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

    @field_validator("target_url")
    @classmethod
    def _validate_target_url(cls, value: str) -> str:
        """Validates the target URL uses the http/https scheme.

        Args:
            value: The raw target URL.

        Returns:
            str: The validated, stripped target URL.

        Raises:
            ValueError: If the URL does not start with `http://` or `https://`.
        """
        return _validate_url_scheme(value)

    @field_validator("http_method")
    @classmethod
    def _validate_http_method(cls, value: str) -> str:
        """Validates and normalizes the HTTP method.

        Args:
            value: The raw HTTP method string.

        Returns:
            str: The upper-cased, validated HTTP method.

        Raises:
            ValueError: If the method is not one of the allowed values.
        """
        normalized = value.strip().upper()
        if normalized not in _ALLOWED_HTTP_METHODS:
            raise ValueError(
                f"http_method must be one of: {sorted(_ALLOWED_HTTP_METHODS)}"
            )
        return normalized

    @field_validator("secret_key")
    @classmethod
    def _normalize_secret_key(cls, value: Optional[str]) -> Optional[str]:
        """Normalizes a blank secret key to `None`.

        Args:
            value: The raw secret key, if supplied.

        Returns:
            Optional[str]: The stripped secret key, or `None`.
        """
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _validate_auth_requires_secret(self) -> "WebhookCreate":
        """Ensures a non-`NONE` authentication type is given a secret key.

        Returns:
            WebhookCreate: The validated model instance.

        Raises:
            ValueError: If `authentication_type` requires a secret key
                but none was supplied, or if `NONE` is combined with a
                supplied secret key.
        """
        if self.authentication_type != AuthenticationType.NONE and not self.secret_key:
            raise ValueError(
                f"secret_key is required for authentication_type "
                f"'{self.authentication_type.value}'."
            )
        if self.authentication_type == AuthenticationType.NONE and self.secret_key:
            raise ValueError(
                "secret_key must not be supplied when authentication_type is 'none'."
            )
        return self


# ---------------------------------------------------------------------------
# Webhook Update Schema
# ---------------------------------------------------------------------------
class WebhookUpdate(BaseModel):
    """Schema used to partially update an existing Webhook (PATCH semantics).

    All fields are optional; only fields explicitly supplied by the
    caller are intended to be applied by the service layer.

    Attributes:
        name: Updated webhook name.
        event: Updated domain event.
        target_url: Updated destination URL.
        http_method: Updated HTTP method.
        status: Updated lifecycle status.
        authentication_type: Updated authentication/signing mechanism.
        secret_key: Updated raw secret.
        custom_headers: Updated static HTTP headers.
        payload_template: Updated payload template.
        retry_count: Updated maximum retry count.
        timeout_seconds: Updated per-request timeout.
        rate_limit_per_minute: Updated rate limit.
        enabled: Updated on/off toggle.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: Optional[str] = Field(
        default=None, min_length=MIN_NAME_LENGTH, max_length=MAX_NAME_LENGTH
    )
    event: Optional[WebhookEvent] = Field(default=None)
    target_url: Optional[str] = Field(default=None, max_length=MAX_TARGET_URL_LENGTH)
    http_method: Optional[str] = Field(default=None)
    status: Optional[WebhookStatus] = Field(default=None)
    authentication_type: Optional[AuthenticationType] = Field(default=None)
    secret_key: Optional[str] = Field(default=None, max_length=MAX_SECRET_KEY_LENGTH)
    custom_headers: Optional[dict[str, str]] = Field(default=None)
    payload_template: Optional[dict[str, Any]] = Field(default=None)
    retry_count: Optional[int] = Field(
        default=None, ge=MIN_RETRY_COUNT, le=MAX_RETRY_COUNT
    )
    timeout_seconds: Optional[int] = Field(
        default=None, ge=MIN_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS
    )
    rate_limit_per_minute: Optional[int] = Field(default=None, gt=0)
    enabled: Optional[bool] = Field(default=None)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: Optional[str]) -> Optional[str]:
        """Ensures a supplied name is not blank after stripping whitespace.

        Args:
            value: The raw name text, if supplied.

        Returns:
            Optional[str]: The validated, stripped name, or `None`.

        Raises:
            ValueError: If a non-`None` value is empty after stripping.
        """
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank.")
        return stripped

    @field_validator("target_url")
    @classmethod
    def _validate_target_url(cls, value: Optional[str]) -> Optional[str]:
        """Validates a supplied target URL uses the http/https scheme.

        Args:
            value: The raw target URL, if supplied.

        Returns:
            Optional[str]: The validated, stripped target URL, or `None`.

        Raises:
            ValueError: If a non-`None` URL does not start with
                `http://` or `https://`.
        """
        if value is None:
            return None
        return _validate_url_scheme(value)

    @field_validator("http_method")
    @classmethod
    def _validate_http_method(cls, value: Optional[str]) -> Optional[str]:
        """Validates and normalizes a supplied HTTP method.

        Args:
            value: The raw HTTP method string, if supplied.

        Returns:
            Optional[str]: The upper-cased, validated HTTP method, or `None`.

        Raises:
            ValueError: If a non-`None` value is not one of the
                allowed HTTP methods.
        """
        if value is None:
            return None
        normalized = value.strip().upper()
        if normalized not in _ALLOWED_HTTP_METHODS:
            raise ValueError(
                f"http_method must be one of: {sorted(_ALLOWED_HTTP_METHODS)}"
            )
        return normalized

    @field_validator("secret_key")
    @classmethod
    def _normalize_secret_key(cls, value: Optional[str]) -> Optional[str]:
        """Normalizes a blank secret key to `None`.

        Args:
            value: The raw secret key, if supplied.

        Returns:
            Optional[str]: The stripped secret key, or `None`.
        """
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _validate_auth_requires_secret(self) -> "WebhookUpdate":
        """Ensures a supplied `authentication_type` of `NONE` is not
        combined with a supplied `secret_key`.

        This partial-update validator intentionally does NOT require a
        secret when `authentication_type` is set to a non-`NONE` value,
        since the record may already have a `secret_key` on file from
        creation; that cross-field completeness check belongs to the
        service layer, which has access to the existing persisted
        record.

        Returns:
            WebhookUpdate: The validated model instance.

        Raises:
            ValueError: If `authentication_type` is explicitly set to
                `NONE` while a non-blank `secret_key` is also supplied
                in the same request.
        """
        if (
            self.authentication_type == AuthenticationType.NONE
            and self.secret_key
        ):
            raise ValueError(
                "secret_key must not be supplied when authentication_type is 'none'."
            )
        return self


# ---------------------------------------------------------------------------
# Webhook Response Schema
# ---------------------------------------------------------------------------
class WebhookResponse(BaseModel):
    """Schema representing a persisted `Webhook` record.

    Note: this schema intentionally has NO `secret_key` field, so raw
    signing/authentication secret material can never be serialized
    back to a caller through this response model.

    Attributes:
        id: Surrogate primary key of the webhook.
        name: Human-readable, unique webhook name.
        event: The domain event this webhook fires on.
        target_url: Destination URL events are delivered to.
        http_method: HTTP method used for delivery.
        status: Current operational lifecycle status.
        authentication_type: Authentication/signing mechanism used.
        custom_headers: Additional static HTTP headers sent with every
            delivery attempt.
        payload_template: Payload shaping template, if configured.
        retry_count: Maximum number of delivery retries on failure.
        timeout_seconds: Per-request delivery timeout, in seconds.
        rate_limit_per_minute: Maximum delivery attempts per minute,
            if enforced.
        enabled: Quick on/off toggle for delivery eligibility.
        last_delivery_at: Timestamp of the most recent delivery attempt.
        last_success_at: Timestamp of the most recent successful delivery.
        last_failure_at: Timestamp of the most recent failed delivery.
        created_by: Identifier of the user who registered this webhook.
        is_deleted: Soft-delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Record creation timestamp.
        updated_at: Record last-update timestamp.
        has_secret_key: Derived flag indicating whether a secret key is
            on file, without ever exposing its content.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    event: WebhookEvent
    target_url: str
    http_method: str
    status: WebhookStatus
    authentication_type: AuthenticationType
    custom_headers: Optional[dict[str, str]] = None
    payload_template: Optional[dict[str, Any]] = None
    retry_count: int
    timeout_seconds: int
    rate_limit_per_minute: Optional[int] = None
    enabled: bool
    last_delivery_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_failure_at: Optional[datetime] = None
    created_by: Optional[int] = Field(default=None, validation_alias="created_by_id")
    is_deleted: bool = False
    deleted_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    has_secret_key: bool = Field(
        default=False,
        description=(
            "Whether a secret key is on file, without exposing it. Not "
            "derivable via `from_attributes` alone (the response schema "
            "has no `secret_key` attribute to read); the service layer "
            "is responsible for setting this explicitly, e.g. via "
            "`WebhookResponse.model_validate(record, update={"
            "'has_secret_key': record.secret_key is not None})` or "
            "equivalent, when constructing this response from a "
            "`Webhook` ORM instance."
        ),
    )


# ---------------------------------------------------------------------------
# Webhook List Response Schema
# ---------------------------------------------------------------------------
class WebhookListResponse(BaseModel):
    """Schema representing a paginated collection of Webhook records.

    Attributes:
        items: The webhook records for the current page.
        total: Total number of records matching the query, across all pages.
        page: Current page number (1-indexed).
        page_size: Number of items requested per page.
        total_pages: Total number of pages available.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[WebhookResponse] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _compute_total_pages_if_needed(self) -> "WebhookListResponse":
        """Recomputes ``total_pages`` defensively when it appears inconsistent.

        Returns:
            WebhookListResponse: The validated model instance.
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
# Webhook Filter Schema (Filtering + Pagination + Sorting)
# ---------------------------------------------------------------------------
class WebhookFilter(BaseModel):
    """Schema encapsulating filter, pagination, and sorting criteria for
    listing webhooks.

    Attributes:
        event: Restrict to a specific domain event.
        status: Restrict to a specific lifecycle status.
        authentication_type: Restrict to a specific authentication type.
        enabled: Restrict to enabled/disabled webhooks only.
        is_deleted: Restrict by soft-delete status. Defaults to
            excluding deleted records.
        search: Free-text match against `name`.
        created_from: Inclusive lower bound on `created_at`.
        created_to: Inclusive upper bound on `created_at`.
        page: 1-indexed page number to retrieve.
        page_size: Number of items to retrieve per page.
        sort_by: Field name to sort results by.
        sort_order: Sort direction, either ``"asc"`` or ``"desc"``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    event: Optional[WebhookEvent] = Field(
        default=None, description="Restrict to a specific domain event."
    )
    status: Optional[WebhookStatus] = Field(
        default=None, description="Restrict to a specific lifecycle status."
    )
    authentication_type: Optional[AuthenticationType] = Field(
        default=None, description="Restrict to a specific authentication type."
    )
    enabled: Optional[bool] = Field(
        default=None, description="Restrict to enabled/disabled webhooks."
    )
    is_deleted: Optional[bool] = Field(
        default=False,
        description="Filter by soft-delete status. Defaults to excluding deleted records.",
    )
    search: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=150,
        description="Free-text match against the webhook name.",
    )
    created_from: Optional[datetime] = Field(
        default=None, description="Inclusive lower bound on created_at."
    )
    created_to: Optional[datetime] = Field(
        default=None, description="Inclusive upper bound on created_at."
    )
    page: int = Field(default=1, ge=1, description="1-indexed page number.")
    page_size: int = Field(
        default=20, ge=1, le=100, description="Number of items per page."
    )
    sort_by: str = Field(
        default="created_at", description="Field name to sort results by."
    )
    sort_order: str = Field(
        default="desc", description="Sort direction: 'asc' or 'desc'."
    )

    @field_validator("search")
    @classmethod
    def _validate_search_not_blank(cls, value: Optional[str]) -> Optional[str]:
        """Rejects a search term that is empty after whitespace stripping.

        Args:
            value: The raw search term, if supplied.

        Returns:
            Optional[str]: The validated, stripped search term, or `None`.

        Raises:
            ValueError: If the string is empty after stripping.
        """
        if value is not None and not value.strip():
            raise ValueError("search must not be empty or whitespace only.")
        return value

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
        if value not in _WEBHOOK_ALLOWED_SORT_FIELDS:
            raise ValueError(
                f"sort_by must be one of: {sorted(_WEBHOOK_ALLOWED_SORT_FIELDS)}"
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

    @model_validator(mode="after")
    def _validate_date_range(self) -> "WebhookFilter":
        """Ensures the provided creation-date range is chronologically valid.

        Returns:
            WebhookFilter: The validated model instance.

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
# Webhook Log Response Schema
# ---------------------------------------------------------------------------
class WebhookLogResponse(BaseModel):
    """Schema representing a persisted `WebhookLog` delivery attempt record.

    Attributes:
        id: Surrogate primary key of the delivery log entry.
        webhook_id: Identifier of the parent webhook.
        delivery_status: Outcome of this delivery attempt.
        response_code: HTTP status code returned by the target, if any.
        response_body: Raw response body returned by the target, if any.
        attempt_count: The 1-indexed attempt number this row represents.
        duration_ms: Wall-clock duration of this delivery attempt, in
            milliseconds.
        error_message: Human-readable error detail, if the attempt failed.
        delivered_at: Timestamp this delivery attempt was made.
        created_at: Record creation timestamp.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    webhook_id: uuid.UUID
    delivery_status: DeliveryStatus
    response_code: Optional[int] = None
    response_body: Optional[str] = None
    attempt_count: int = 1
    duration_ms: Optional[float] = None
    error_message: Optional[str] = None
    delivered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Webhook Log List Response Schema
# ---------------------------------------------------------------------------
class WebhookLogListResponse(BaseModel):
    """Schema representing a paginated collection of WebhookLog records.

    Attributes:
        items: The delivery log records for the current page.
        total: Total number of records matching the query, across all pages.
        page: Current page number (1-indexed).
        page_size: Number of items requested per page.
        total_pages: Total number of pages available.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[WebhookLogResponse] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _compute_total_pages_if_needed(self) -> "WebhookLogListResponse":
        """Recomputes ``total_pages`` defensively when it appears inconsistent.

        Returns:
            WebhookLogListResponse: The validated model instance.
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
# Webhook Log Filter Schema (Filtering + Pagination + Sorting)
# ---------------------------------------------------------------------------
class WebhookLogFilter(BaseModel):
    """Schema encapsulating filter, pagination, and sorting criteria for
    listing a webhook's delivery logs.

    Attributes:
        webhook_id: Restrict to logs belonging to a specific webhook.
        delivery_status: Restrict to a specific delivery outcome.
        delivered_from: Inclusive lower bound on `delivered_at`.
        delivered_to: Inclusive upper bound on `delivered_at`.
        page: 1-indexed page number to retrieve.
        page_size: Number of items to retrieve per page.
        sort_by: Field name to sort results by.
        sort_order: Sort direction, either ``"asc"`` or ``"desc"``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    webhook_id: Optional[uuid.UUID] = Field(
        default=None, description="Restrict to logs belonging to a specific webhook."
    )
    delivery_status: Optional[DeliveryStatus] = Field(
        default=None, description="Restrict to a specific delivery outcome."
    )
    delivered_from: Optional[datetime] = Field(
        default=None, description="Inclusive lower bound on delivered_at."
    )
    delivered_to: Optional[datetime] = Field(
        default=None, description="Inclusive upper bound on delivered_at."
    )
    page: int = Field(default=1, ge=1, description="1-indexed page number.")
    page_size: int = Field(
        default=20, ge=1, le=100, description="Number of items per page."
    )
    sort_by: str = Field(
        default="delivered_at", description="Field name to sort results by."
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
        if value not in _WEBHOOK_LOG_ALLOWED_SORT_FIELDS:
            raise ValueError(
                f"sort_by must be one of: {sorted(_WEBHOOK_LOG_ALLOWED_SORT_FIELDS)}"
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

    @model_validator(mode="after")
    def _validate_date_range(self) -> "WebhookLogFilter":
        """Ensures the provided delivery-date range is chronologically valid.

        Returns:
            WebhookLogFilter: The validated model instance.

        Raises:
            ValueError: If `delivered_from` is after `delivered_to`.
        """
        if (
            self.delivered_from
            and self.delivered_to
            and self.delivered_from > self.delivered_to
        ):
            raise ValueError("delivered_from must not be after delivered_to.")
        return self


# ---------------------------------------------------------------------------
# Webhook Statistics Response Schema
# ---------------------------------------------------------------------------
class WebhookStatisticsResponse(BaseModel):
    """Schema representing aggregate delivery statistics for a webhook
    (or across a set of webhooks, depending on the service-layer scope
    used to compute it).

    Attributes:
        total_webhooks: Total number of webhooks in scope.
        active_count: Number of webhooks with status `active`.
        suspended_count: Number of webhooks with status `suspended`.
        failed_count: Number of webhooks with status `failed`.
        by_event: Count of webhooks grouped by `event`.
        by_status: Count of webhooks grouped by `status`.
        total_deliveries: Total number of delivery attempts (log rows)
            in scope.
        successful_deliveries: Number of deliveries with
            `delivery_status` `success`.
        failed_deliveries: Number of deliveries with `delivery_status`
            `failed`.
        dead_lettered_deliveries: Number of deliveries with
            `delivery_status` `dead_lettered`.
        success_rate_percentage: Percentage of deliveries that were
            successful, out of total deliveries in scope.
        average_duration_ms: Average delivery duration across
            deliveries in scope, in milliseconds.
        last_delivery_at: Most recent `delivered_at` observed across
            deliveries in scope, if any.
        generated_at: Timestamp when these statistics were computed.
    """

    model_config = ConfigDict(from_attributes=True)

    total_webhooks: int = Field(default=0, ge=0)
    active_count: int = Field(default=0, ge=0)
    suspended_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    by_event: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    total_deliveries: int = Field(default=0, ge=0)
    successful_deliveries: int = Field(default=0, ge=0)
    failed_deliveries: int = Field(default=0, ge=0)
    dead_lettered_deliveries: int = Field(default=0, ge=0)
    success_rate_percentage: Optional[float] = Field(default=None, ge=0, le=100)
    average_duration_ms: Optional[float] = Field(default=None, ge=0)
    last_delivery_at: Optional[datetime] = None
    generated_at: datetime