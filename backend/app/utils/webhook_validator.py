"""
backend/app/utils/webhook_validator.py

Reusable, framework-agnostic validation helpers for the Enterprise
Webhook module of the Enterprise Real Estate AI Copilot CRM.

This module is the single canonical location for the module's
business-rule validation logic (target URL / SSRF guarding, event,
secret, headers, payload template, HTTP method, timeout, and retry
configuration). It has no dependency on FastAPI, SQLAlchemy sessions,
or the Repository/Service layers, so it can be safely imported by:
    - `app.services.webhook_service.WebhookService` (request-time
      validation before persistence),
    - `app.utils.webhook_dispatcher.WebhookDispatcher` (defensive
      re-validation immediately before an outbound delivery attempt),
    - `app.api.v1.webhook` (light input sanity checks, where useful,
      ahead of calling the Service layer).

Raises ONLY the project's domain exceptions (never
`fastapi.HTTPException`), matching the convention already used by
`app.services.webhook_service`.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from typing import Any, Optional
from urllib.parse import urlparse

from app.core.exceptions import ValidationException
from app.models.webhook import AuthenticationType, WebhookEvent

__all__ = ["WebhookValidator"]

# ---------------------------------------------------------------------------
# Shared Validation Constants
# ---------------------------------------------------------------------------
_ALLOWED_HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE"}
)

_MIN_TIMEOUT_SECONDS: int = 1
_MAX_TIMEOUT_SECONDS: int = 300

_MIN_RETRY_COUNT: int = 0
_MAX_RETRY_COUNT: int = 10

_MIN_SECRET_LENGTH: int = 8
_MAX_SECRET_LENGTH: int = 255

_MAX_CUSTOM_HEADERS: int = 20
_MAX_HEADER_VALUE_LENGTH: int = 2048
_FORBIDDEN_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "authorization",
        "x-webhook-signature",
    }
)

_MAX_PAYLOAD_TEMPLATE_KEYS: int = 100
_MAX_PAYLOAD_TEMPLATE_BYTES: int = 65_536

_MIN_RATE_LIMIT_PER_MINUTE: int = 1

#: Domain event categories the Webhook module's product scope covers.
#: `WebhookEvent` (the persisted, DB-backed enum) implements a subset
#: of these plus `CUSTOM` as a catch-all; this mapping exists purely
#: for descriptive grouping/statistics and is never used to bypass the
#: DB enum itself.
EVENT_CATEGORY_MAP: dict[WebhookEvent, str] = {
    WebhookEvent.LEAD_CREATED: "Lead",
    WebhookEvent.LEAD_UPDATED: "Lead",
    WebhookEvent.LEAD_CONVERTED: "Lead",
    WebhookEvent.DEAL_CREATED: "Customer",
    WebhookEvent.DEAL_UPDATED: "Customer",
    WebhookEvent.DEAL_CLOSED: "Customer",
    WebhookEvent.TASK_CREATED: "Task",
    WebhookEvent.TASK_COMPLETED: "Task",
    WebhookEvent.DOCUMENT_UPLOADED: "Document",
    WebhookEvent.PAYMENT_RECEIVED: "Payment",
    WebhookEvent.BOOKING_CREATED: "Booking",
    WebhookEvent.BOOKING_CANCELLED: "Booking",
    WebhookEvent.USER_CREATED: "Activity",
    WebhookEvent.CUSTOM: "Integration",
}

#: Loopback / private / link-local / reserved ranges outbound webhook
#: targets are not allowed to resolve to, as a baseline SSRF guard.
_BLOCKED_IP_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


class WebhookValidator:
    """Namespace of stateless, static business-rule validators for the
    Webhook module. All methods return the validated value unchanged on
    success and raise `ValidationException` on failure, so calls can be
    chained inline (e.g. `value = WebhookValidator.validate_timeout(value)`).
    """

    # ------------------------------------------------------------------
    # Target URL (with baseline SSRF guarding)
    # ------------------------------------------------------------------
    @staticmethod
    def validate_target_url(target_url: str) -> str:
        """Validates a webhook target URL's scheme, hostname, and
        resolved address space.

        Args:
            target_url: The candidate destination URL.

        Returns:
            str: The validated target URL, unchanged.

        Raises:
            ValidationException: If the URL is malformed, uses a
                disallowed scheme, has no hostname, cannot be
                resolved, or resolves to a private/loopback/
                link-local/reserved network address.
        """
        if not target_url or not target_url.strip():
            raise ValidationException("target_url must not be blank.")

        parsed = urlparse(target_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValidationException(
                "target_url must use the 'http' or 'https' scheme."
            )
        if not parsed.hostname:
            raise ValidationException("target_url must include a valid hostname.")

        try:
            resolved_addresses = {
                info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)
            }
        except socket.gaierror as exc:
            raise ValidationException(
                f"target_url hostname '{parsed.hostname}' could not be resolved."
            ) from exc

        for address in resolved_addresses:
            ip = ipaddress.ip_address(address)
            if any(ip in network for network in _BLOCKED_IP_NETWORKS):
                raise ValidationException(
                    "target_url must not resolve to a private, loopback, "
                    "link-local, or reserved network address."
                )

        return target_url

    # ------------------------------------------------------------------
    # Event
    # ------------------------------------------------------------------
    @staticmethod
    def validate_event(event: WebhookEvent) -> WebhookEvent:
        """Validates a webhook's subscribed domain event.

        Args:
            event: The candidate `WebhookEvent`.

        Returns:
            WebhookEvent: The validated event, unchanged.

        Raises:
            ValidationException: If `event` is not a recognized
                `WebhookEvent` member.
        """
        if not isinstance(event, WebhookEvent):
            raise ValidationException(f"'{event}' is not a supported webhook event.")
        return event

    @staticmethod
    def get_event_category(event: WebhookEvent) -> str:
        """Returns the descriptive product category for a domain event.

        Args:
            event: The webhook event.

        Returns:
            str: The category label (e.g. `"Lead"`, `"Payment"`), used
            for statistics grouping/UI display only.
        """
        return EVENT_CATEGORY_MAP.get(event, "Integration")

    # ------------------------------------------------------------------
    # Authentication / Secret
    # ------------------------------------------------------------------
    @staticmethod
    def validate_secret(
        authentication_type: AuthenticationType, secret_key: Optional[str]
    ) -> Optional[str]:
        """Validates the authentication-type/secret pairing and, where a
        secret is required, its minimum strength/length.

        Args:
            authentication_type: The requested authentication mechanism.
            secret_key: The raw secret supplied, if any.

        Returns:
            Optional[str]: The validated secret key, unchanged.

        Raises:
            ValidationException: If a non-`NONE` authentication type is
                missing a secret (or the secret is too short/long), or
                `NONE` is combined with a supplied secret.
        """
        if authentication_type == AuthenticationType.NONE:
            if secret_key:
                raise ValidationException(
                    "secret_key must not be supplied when authentication_type "
                    "is 'none'."
                )
            return secret_key

        if not secret_key:
            raise ValidationException(
                f"secret_key is required for authentication_type "
                f"'{authentication_type.value}'."
            )
        if len(secret_key) < _MIN_SECRET_LENGTH:
            raise ValidationException(
                f"secret_key must be at least {_MIN_SECRET_LENGTH} characters "
                f"for authentication_type '{authentication_type.value}'."
            )
        if len(secret_key) > _MAX_SECRET_LENGTH:
            raise ValidationException(
                f"secret_key must not exceed {_MAX_SECRET_LENGTH} characters."
            )
        return secret_key

    # ------------------------------------------------------------------
    # Custom Headers
    # ------------------------------------------------------------------
    @staticmethod
    def validate_headers(
        custom_headers: Optional[dict[str, str]]
    ) -> Optional[dict[str, str]]:
        """Validates caller-supplied static delivery headers.

        Args:
            custom_headers: Candidate static headers, if any.

        Returns:
            Optional[dict[str, str]]: The validated headers, unchanged.

        Raises:
            ValidationException: If too many headers are supplied, a
                header name is blank/reserved/forbidden, or a header
                value exceeds the allowed length.
        """
        if not custom_headers:
            return custom_headers

        if len(custom_headers) > _MAX_CUSTOM_HEADERS:
            raise ValidationException(
                f"custom_headers may not contain more than "
                f"{_MAX_CUSTOM_HEADERS} entries."
            )

        for name, value in custom_headers.items():
            if not name or not name.strip():
                raise ValidationException("custom_headers keys must not be blank.")
            if name.strip().lower() in _FORBIDDEN_HEADER_NAMES:
                raise ValidationException(
                    f"custom_headers must not override the reserved header "
                    f"'{name}'."
                )
            if len(value) > _MAX_HEADER_VALUE_LENGTH:
                raise ValidationException(
                    f"custom_headers['{name}'] exceeds the maximum length of "
                    f"{_MAX_HEADER_VALUE_LENGTH} characters."
                )

        return custom_headers

    # ------------------------------------------------------------------
    # Payload Template
    # ------------------------------------------------------------------
    @staticmethod
    def validate_payload(
        payload_template: Optional[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        """Validates a caller-supplied outbound payload-shaping template.

        Args:
            payload_template: Candidate JSON template, if any.

        Returns:
            Optional[dict[str, Any]]: The validated template, unchanged.

        Raises:
            ValidationException: If the template is not
                JSON-serializable, exceeds the maximum key count, or
                exceeds the maximum serialized size.
        """
        if payload_template is None:
            return None

        if len(payload_template) > _MAX_PAYLOAD_TEMPLATE_KEYS:
            raise ValidationException(
                f"payload_template may not contain more than "
                f"{_MAX_PAYLOAD_TEMPLATE_KEYS} top-level keys."
            )

        try:
            serialized = json.dumps(payload_template)
        except (TypeError, ValueError) as exc:
            raise ValidationException(
                "payload_template must be JSON-serializable."
            ) from exc

        if len(serialized.encode("utf-8")) > _MAX_PAYLOAD_TEMPLATE_BYTES:
            raise ValidationException(
                f"payload_template must not exceed "
                f"{_MAX_PAYLOAD_TEMPLATE_BYTES} bytes when serialized."
            )

        return payload_template

    # ------------------------------------------------------------------
    # HTTP Method
    # ------------------------------------------------------------------
    @staticmethod
    def validate_http_method(http_method: str) -> str:
        """Validates and normalizes the delivery HTTP method.

        Args:
            http_method: The raw HTTP method string.

        Returns:
            str: The upper-cased, validated HTTP method.

        Raises:
            ValidationException: If the method is not one of the
                allowed values.
        """
        normalized = (http_method or "").strip().upper()
        if normalized not in _ALLOWED_HTTP_METHODS:
            raise ValidationException(
                f"http_method must be one of: {sorted(_ALLOWED_HTTP_METHODS)}"
            )
        return normalized

    # ------------------------------------------------------------------
    # Timeout
    # ------------------------------------------------------------------
    @staticmethod
    def validate_timeout(timeout_seconds: int) -> int:
        """Validates the per-request delivery timeout.

        Args:
            timeout_seconds: The candidate timeout, in seconds.

        Returns:
            int: The validated timeout, unchanged.

        Raises:
            ValidationException: If the timeout is outside the allowed
                `[1, 300]` second range.
        """
        if not (_MIN_TIMEOUT_SECONDS <= timeout_seconds <= _MAX_TIMEOUT_SECONDS):
            raise ValidationException(
                f"timeout_seconds must be between {_MIN_TIMEOUT_SECONDS} and "
                f"{_MAX_TIMEOUT_SECONDS} seconds."
            )
        return timeout_seconds

    # ------------------------------------------------------------------
    # Retry / Rate-Limit Configuration
    # ------------------------------------------------------------------
    @staticmethod
    def validate_retry_configuration(
        retry_count: int,
        timeout_seconds: int,
        rate_limit_per_minute: Optional[int] = None,
    ) -> None:
        """Validates the delivery retry/timeout/rate-limit policy as a
        coherent whole.

        Args:
            retry_count: Maximum number of delivery retries on failure.
            timeout_seconds: Per-request delivery timeout, in seconds.
            rate_limit_per_minute: Maximum delivery attempts permitted
                per minute, if enforced.

        Raises:
            ValidationException: If `retry_count` is outside
                `[0, 10]`, if `rate_limit_per_minute` is supplied and
                not a positive integer, or if the combination of
                `retry_count` and `timeout_seconds` could allow a
                single delivery's total worst-case retry duration to
                exceed a sane operational ceiling (30 minutes).
        """
        if not (_MIN_RETRY_COUNT <= retry_count <= _MAX_RETRY_COUNT):
            raise ValidationException(
                f"retry_count must be between {_MIN_RETRY_COUNT} and "
                f"{_MAX_RETRY_COUNT}."
            )

        if rate_limit_per_minute is not None and rate_limit_per_minute < _MIN_RATE_LIMIT_PER_MINUTE:
            raise ValidationException(
                f"rate_limit_per_minute must be at least "
                f"{_MIN_RATE_LIMIT_PER_MINUTE} when supplied."
            )

        worst_case_seconds = retry_count * timeout_seconds
        if worst_case_seconds > 1800:
            raise ValidationException(
                "The combination of retry_count and timeout_seconds implies "
                "a worst-case delivery window greater than 30 minutes; "
                "reduce retry_count or timeout_seconds."
            )

    # ------------------------------------------------------------------
    # Aggregate Entry Points
    # ------------------------------------------------------------------
    @classmethod
    def validate_all(
        cls,
        *,
        target_url: str,
        event: WebhookEvent,
        http_method: str,
        authentication_type: AuthenticationType,
        secret_key: Optional[str],
        custom_headers: Optional[dict[str, str]],
        payload_template: Optional[dict[str, Any]],
        retry_count: int,
        timeout_seconds: int,
        rate_limit_per_minute: Optional[int] = None,
    ) -> None:
        """Runs every business-rule validator for a full webhook
        configuration in one call.

        Args:
            target_url: Destination URL events are delivered to.
            event: The domain event this webhook fires on.
            http_method: HTTP method used for delivery.
            authentication_type: Authentication/signing mechanism.
            secret_key: Raw secret used for signing/auth, if any.
            custom_headers: Additional static HTTP headers, if any.
            payload_template: Optional outbound payload-shaping template.
            retry_count: Maximum number of delivery retries on failure.
            timeout_seconds: Per-request delivery timeout, in seconds.
            rate_limit_per_minute: Maximum delivery attempts per
                minute, if enforced.

        Raises:
            ValidationException: If any individual business rule fails.
        """
        cls.validate_target_url(target_url)
        cls.validate_event(event)
        cls.validate_http_method(http_method)
        cls.validate_secret(authentication_type, secret_key)
        cls.validate_headers(custom_headers)
        cls.validate_payload(payload_template)
        cls.validate_timeout(timeout_seconds)
        cls.validate_retry_configuration(
            retry_count, timeout_seconds, rate_limit_per_minute
        )