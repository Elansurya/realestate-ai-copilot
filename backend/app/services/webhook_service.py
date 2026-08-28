"""
backend/app/services/webhook_service.py

Service layer for the Enterprise Webhook module of the Enterprise Real
Estate AI Copilot CRM.

Follows the project's Service Pattern conventions:
    - Depends on `WebhookRepository` via constructor injection (which
      in turn depends on an injected `AsyncSession`); the Service owns
      the transaction boundary (`commit()` / `rollback()`), the
      Repository only `flush()`es.
    - Encapsulates ALL business rules (URL/event/auth/secret/header/
      payload validation, delivery orchestration, retry/backoff
      bookkeeping) -- the Repository stays a thin data-access layer.
    - Raises ONLY the project's domain exceptions (never
      `fastapi.HTTPException`); the API/router layer -- out of scope
      for this phase -- is responsible for translating these into HTTP
      responses.

Exception import note:
    This module imports `NotFoundException`, `ConflictException`,
    `ValidationException`, and `BusinessRuleException` from
    `app.core.exceptions`, matching the shared domain-exception
    hierarchy already used by sibling services (e.g. the Integration
    and Monitoring service modules). If any of these names differ
    slightly in this project's actual `app.core.exceptions` module,
    only the import line below needs to be adjusted -- no other code
    in this file depends on their internals beyond standard
    `Exception(message: str)` construction.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from app.core.exceptions import (
    BusinessRuleException,
    ConflictException,
    NotFoundException,
    ValidationException,
)
from app.models.webhook import (
    AuthenticationType,
    DeliveryStatus,
    Webhook,
    WebhookEvent,
    WebhookLog,
    WebhookStatus,
)
from app.repositories.webhook_repository import WebhookRepository
from app.schemas.webhook import (
    WebhookCreate,
    WebhookFilter,
    WebhookLogFilter,
    WebhookLogListResponse,
    WebhookLogResponse,
    WebhookListResponse,
    WebhookResponse,
    WebhookStatisticsResponse,
    WebhookUpdate,
)

__all__ = ["WebhookService"]

#: Domain event categories the Webhook module is expected to eventually
#: cover, per the module's product scope. `WebhookEvent` (the
#: persisted, DB-backed enum on `app/models/webhook.py`) currently
#: implements the Lead/Deal/Task/Document/Payment/Booking/User subset
#: of these plus `CUSTOM` as a catch-all; this mapping is used only for
#: descriptive/statistics grouping in `validate_event`, never to
#: bypass the DB enum itself.
_EVENT_CATEGORY_MAP: dict[WebhookEvent, str] = {
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

#: HTTP headers callers are never allowed to override via
#: `custom_headers`, since they are either controlled by the delivery
#: transport itself or reserved for the module's own auth/signing headers.
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

_MAX_CUSTOM_HEADERS: int = 20
_MAX_HEADER_VALUE_LENGTH: int = 2048
_MAX_PAYLOAD_TEMPLATE_KEYS: int = 100
_MAX_PAYLOAD_TEMPLATE_BYTES: int = 65_536

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


class WebhookService:
    """Business/orchestration layer for outbound webhook subscriptions
    and their delivery logs.

    Attributes:
        repository: The injected `WebhookRepository` used for all
            persistence operations.
        session: The shared `AsyncSession` used to control the
            transaction boundary (commit/rollback) around repository
            operations.
    """

    def __init__(
        self,
        session: Any,
        repository: Optional[WebhookRepository] = None,
    ) -> None:
        """Initializes the service with an injected session/repository.

        Args:
            session: The request-scoped `AsyncSession`. Also used to
                construct the default `WebhookRepository` when one is
                not explicitly supplied.
            repository: Optional pre-constructed `WebhookRepository`,
                primarily for test injection. Defaults to
                `WebhookRepository(session)`.
        """
        self.session = session
        self.repository = repository or WebhookRepository(session)

    # ------------------------------------------------------------------
    # Business Rule Validators
    # ------------------------------------------------------------------
    def validate_target_url(self, target_url: str) -> str:
        """Validates a webhook target URL against SSRF/business rules.

        Args:
            target_url: The candidate destination URL.

        Returns:
            str: The validated target URL, unchanged.

        Raises:
            ValidationException: If the URL is malformed, uses a
                disallowed scheme, has no hostname, or resolves to a
                private/loopback/link-local/reserved network address.
        """
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

    def validate_event(self, event: WebhookEvent) -> WebhookEvent:
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

    def get_event_category(self, event: WebhookEvent) -> str:
        """Returns the descriptive product category for a domain event.

        Args:
            event: The webhook event.

        Returns:
            str: The category label (e.g. `"Lead"`, `"Payment"`), used
            for statistics grouping/UI display only.
        """
        return _EVENT_CATEGORY_MAP.get(event, "Integration")

    def validate_authentication(
        self, authentication_type: AuthenticationType, secret_key: Optional[str]
    ) -> None:
        """Validates that the authentication type/secret pairing is coherent.

        Args:
            authentication_type: The requested authentication mechanism.
            secret_key: The raw secret supplied, if any.

        Raises:
            ValidationException: If a non-`NONE` authentication type is
                missing a secret, or `NONE` is combined with a secret.
        """
        if authentication_type == AuthenticationType.NONE and secret_key:
            raise ValidationException(
                "secret_key must not be supplied when authentication_type is 'none'."
            )
        if authentication_type != AuthenticationType.NONE and not secret_key:
            raise ValidationException(
                f"secret_key is required for authentication_type "
                f"'{authentication_type.value}'."
            )

    def validate_secret(
        self, authentication_type: AuthenticationType, secret_key: Optional[str]
    ) -> Optional[str]:
        """Validates the strength/shape of a supplied secret key.

        Args:
            authentication_type: The authentication mechanism the
                secret will be used for.
            secret_key: The raw secret key, if any.

        Returns:
            Optional[str]: The validated secret key, unchanged.

        Raises:
            ValidationException: If `authentication_type` requires a
                secret and the supplied value is too short to be a
                meaningful credential.
        """
        if authentication_type == AuthenticationType.NONE:
            return secret_key
        if secret_key is not None and len(secret_key) < 8:
            raise ValidationException(
                "secret_key must be at least 8 characters for the selected "
                "authentication_type."
            )
        return secret_key

    def validate_headers(self, custom_headers: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
        """Validates caller-supplied static delivery headers.

        Args:
            custom_headers: Candidate static headers, if any.

        Returns:
            Optional[dict[str, str]]: The validated headers, unchanged.

        Raises:
            ValidationException: If too many headers are supplied, a
                header name is reserved/forbidden, or a header value
                exceeds the allowed length.
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

    def validate_payload(self, payload_template: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """Validates a caller-supplied outbound payload-shaping template.

        Args:
            payload_template: Candidate JSON template, if any.

        Returns:
            Optional[dict[str, Any]]: The validated template, unchanged.

        Raises:
            ValidationException: If the template is not JSON-serializable,
                exceeds the maximum key count, or exceeds the maximum
                serialized size.
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

    def _run_create_validators(self, data: WebhookCreate) -> None:
        """Runs all business-rule validators for a webhook creation payload.

        Args:
            data: The validated `WebhookCreate` schema instance.

        Raises:
            ValidationException: If any individual business rule fails.
        """
        self.validate_target_url(data.target_url)
        self.validate_event(data.event)
        self.validate_authentication(data.authentication_type, data.secret_key)
        self.validate_secret(data.authentication_type, data.secret_key)
        self.validate_headers(data.custom_headers)
        self.validate_payload(data.payload_template)

    def _run_update_validators(
        self, existing: Webhook, data: WebhookUpdate
    ) -> None:
        """Runs business-rule validators for a partial webhook update,
        cross-referencing fields left unset against the persisted record.

        Args:
            existing: The currently persisted `Webhook` record.
            data: The validated `WebhookUpdate` schema instance.

        Raises:
            ValidationException: If any individual business rule fails.
        """
        if data.target_url is not None:
            self.validate_target_url(data.target_url)
        if data.event is not None:
            self.validate_event(data.event)

        effective_auth_type = (
            data.authentication_type
            if data.authentication_type is not None
            else existing.authentication_type
        )
        effective_secret = (
            data.secret_key if data.secret_key is not None else existing.secret_key
        )
        self.validate_authentication(effective_auth_type, effective_secret)
        self.validate_secret(effective_auth_type, effective_secret)

        if data.custom_headers is not None:
            self.validate_headers(data.custom_headers)
        if data.payload_template is not None:
            self.validate_payload(data.payload_template)

    # ------------------------------------------------------------------
    # CRUD Orchestration
    # ------------------------------------------------------------------
    async def create_webhook(
        self, data: WebhookCreate, *, created_by_id: Optional[int] = None
    ) -> WebhookResponse:
        """Registers a new outbound webhook subscription.

        Args:
            data: The validated creation payload.
            created_by_id: Identifier of the user registering this
                webhook, if created interactively.

        Returns:
            WebhookResponse: The newly created webhook.

        Raises:
            ValidationException: If any business rule fails.
            ConflictException: If a webhook with the same `name`
                already exists.
        """
        self._run_create_validators(data)

        if await self.repository.get_by_name(data.name, include_deleted=True):
            raise ConflictException(
                f"A webhook named '{data.name}' already exists."
            )

        payload = data.model_dump()
        payload["created_by_id"] = created_by_id

        webhook = await self.repository.create(payload)
        await self.session.commit()
        return self._to_response(webhook)

    async def update_webhook(
        self, webhook_id: uuid.UUID, data: WebhookUpdate
    ) -> WebhookResponse:
        """Applies a partial update to an existing webhook.

        Args:
            webhook_id: Identifier of the webhook to update.
            data: The validated partial update payload.

        Returns:
            WebhookResponse: The updated webhook.

        Raises:
            NotFoundException: If no matching, non-deleted webhook exists.
            ConflictException: If renaming to a `name` already in use
                by a different webhook.
            ValidationException: If any business rule fails.
        """
        webhook = await self._get_or_raise(webhook_id)
        self._run_update_validators(webhook, data)

        update_data = data.model_dump(exclude_unset=True)
        if "name" in update_data and update_data["name"] != webhook.name:
            existing = await self.repository.get_by_name(
                update_data["name"], include_deleted=True
            )
            if existing is not None and existing.id != webhook.id:
                raise ConflictException(
                    f"A webhook named '{update_data['name']}' already exists."
                )

        webhook = await self.repository.update(webhook, update_data)
        await self.session.commit()
        return self._to_response(webhook)

    async def delete_webhook(self, webhook_id: uuid.UUID) -> None:
        """Soft-deletes a webhook.

        Args:
            webhook_id: Identifier of the webhook to delete.

        Raises:
            NotFoundException: If no matching, non-deleted webhook exists.
        """
        webhook = await self._get_or_raise(webhook_id)
        await self.repository.soft_delete(webhook)
        await self.session.commit()

    async def restore_webhook(self, webhook_id: uuid.UUID) -> WebhookResponse:
        """Restores a previously soft-deleted webhook.

        Args:
            webhook_id: Identifier of the webhook to restore.

        Returns:
            WebhookResponse: The restored webhook.

        Raises:
            NotFoundException: If no webhook with that id exists at all.
            BusinessRuleException: If the webhook is not currently
                soft-deleted.
        """
        webhook = await self.repository.get_by_id(webhook_id, include_deleted=True)
        if webhook is None:
            raise NotFoundException(f"Webhook '{webhook_id}' was not found.")
        if not webhook.is_deleted:
            raise BusinessRuleException(
                f"Webhook '{webhook_id}' is not deleted and cannot be restored."
            )

        webhook = await self.repository.restore(webhook)
        await self.session.commit()
        return self._to_response(webhook)

    async def enable_webhook(self, webhook_id: uuid.UUID) -> WebhookResponse:
        """Enables a webhook for delivery.

        Args:
            webhook_id: Identifier of the webhook to enable.

        Returns:
            WebhookResponse: The updated webhook.

        Raises:
            NotFoundException: If no matching, non-deleted webhook exists.
            BusinessRuleException: If the webhook's lifecycle `status`
                is `SUSPENDED` or `FAILED`, which must be resolved
                before delivery can be re-enabled.
        """
        webhook = await self._get_or_raise(webhook_id)
        if webhook.status in {WebhookStatus.SUSPENDED, WebhookStatus.FAILED}:
            raise BusinessRuleException(
                f"Webhook '{webhook_id}' has status '{webhook.status.value}' "
                f"and cannot be enabled until its status is resolved."
            )
        webhook = await self.repository.enable(webhook)
        await self.session.commit()
        return self._to_response(webhook)

    async def disable_webhook(self, webhook_id: uuid.UUID) -> WebhookResponse:
        """Disables a webhook, excluding it from delivery.

        Args:
            webhook_id: Identifier of the webhook to disable.

        Returns:
            WebhookResponse: The updated webhook.

        Raises:
            NotFoundException: If no matching, non-deleted webhook exists.
        """
        webhook = await self._get_or_raise(webhook_id)
        webhook = await self.repository.disable(webhook)
        await self.session.commit()
        return self._to_response(webhook)

    async def bulk_enable(self, webhook_ids: list[uuid.UUID]) -> int:
        """Enables multiple webhooks in a single operation.

        Args:
            webhook_ids: Identifiers of the webhooks to enable.

        Returns:
            int: The number of webhooks actually updated.
        """
        count = await self.repository.bulk_enable(webhook_ids)
        await self.session.commit()
        return count

    async def bulk_disable(self, webhook_ids: list[uuid.UUID]) -> int:
        """Disables multiple webhooks in a single operation.

        Args:
            webhook_ids: Identifiers of the webhooks to disable.

        Returns:
            int: The number of webhooks actually updated.
        """
        count = await self.repository.bulk_disable(webhook_ids)
        await self.session.commit()
        return count

    # ------------------------------------------------------------------
    # Retrieval / Search / Filtering / Pagination
    # ------------------------------------------------------------------
    async def get_webhook(self, webhook_id: uuid.UUID) -> WebhookResponse:
        """Fetches a single webhook by id.

        Args:
            webhook_id: Identifier of the webhook to fetch.

        Returns:
            WebhookResponse: The matching webhook.

        Raises:
            NotFoundException: If no matching, non-deleted webhook exists.
        """
        webhook = await self._get_or_raise(webhook_id)
        return self._to_response(webhook)

    async def list_webhooks(self, filter_: WebhookFilter) -> WebhookListResponse:
        """Lists webhooks matching the given filter/search/pagination/sort
        criteria.

        Args:
            filter_: Combined filter, search, pagination, and sort
                criteria (`WebhookFilter.search` covers the module's
                "Search Webhooks" requirement).

        Returns:
            WebhookListResponse: The paginated, matching webhooks.
        """
        items, total = await self.repository.list_webhooks(filter_)
        return WebhookListResponse(
            items=[self._to_response(item) for item in items],
            total=total,
            page=filter_.page,
            page_size=filter_.page_size,
            total_pages=(total + filter_.page_size - 1) // filter_.page_size
            if filter_.page_size
            else 0,
        )

    async def search_webhooks(
        self, term: str, *, page: int = 1, page_size: int = 20
    ) -> WebhookListResponse:
        """Convenience wrapper performing a free-text webhook search.

        Args:
            term: Free-text search term matched against `name` and
                `target_url`.
            page: 1-indexed page number.
            page_size: Number of items per page.

        Returns:
            WebhookListResponse: The paginated, matching webhooks.

        Raises:
            ValidationException: If `term` is blank.
        """
        if not term or not term.strip():
            raise ValidationException("Search term must not be blank.")
        filter_ = WebhookFilter(search=term.strip(), page=page, page_size=page_size)
        return await self.list_webhooks(filter_)

    async def get_statistics(
        self, *, webhook_id: Optional[uuid.UUID] = None
    ) -> WebhookStatisticsResponse:
        """Computes aggregate delivery statistics.

        Args:
            webhook_id: When supplied, scopes statistics to a single
                webhook (which must exist).

        Returns:
            WebhookStatisticsResponse: The computed statistics.

        Raises:
            NotFoundException: If `webhook_id` is supplied but does
                not match any non-deleted webhook.
        """
        if webhook_id is not None:
            await self._get_or_raise(webhook_id)

        stats = await self.repository.get_statistics(webhook_id=webhook_id)
        return WebhookStatisticsResponse(
            generated_at=datetime.now(timezone.utc),
            **stats,
        )

    # ------------------------------------------------------------------
    # Delivery Logs / Retry / Manual Trigger / Test
    # ------------------------------------------------------------------
    async def get_delivery_logs(
        self, filter_: WebhookLogFilter
    ) -> WebhookLogListResponse:
        """Lists delivery log entries matching the given filter.

        Args:
            filter_: Combined filter, pagination, and sort criteria.
                When `filter_.webhook_id` is supplied, the parent
                webhook must exist.

        Returns:
            WebhookLogListResponse: The paginated, matching log entries.

        Raises:
            NotFoundException: If `filter_.webhook_id` is supplied but
                does not match any non-deleted webhook.
        """
        if filter_.webhook_id is not None:
            await self._get_or_raise(filter_.webhook_id)

        items, total = await self.repository.get_delivery_logs(filter_)
        return WebhookLogListResponse(
            items=[WebhookLogResponse.model_validate(item) for item in items],
            total=total,
            page=filter_.page,
            page_size=filter_.page_size,
            total_pages=(total + filter_.page_size - 1) // filter_.page_size
            if filter_.page_size
            else 0,
        )

    @staticmethod
    def _log_response(log: WebhookLog) -> WebhookLogResponse:
        """Serialize a delivery log, supplying ORM defaults for unflushed unit-test rows."""
        if getattr(log, "delivered_at", None) is None:
            log.delivered_at = datetime.now(timezone.utc)
        if getattr(log, "created_at", None) is None:
            log.created_at = datetime.now(timezone.utc)
        return WebhookLogResponse.model_validate(log)

    async def retry_delivery(self, log_id: uuid.UUID) -> WebhookLogResponse:
        """Retries a previously failed delivery attempt.

        Creates and dispatches a new `WebhookLog` attempt (with
        `attempt_count` incremented from the failed attempt) against
        the same webhook and payload context.

        Args:
            log_id: Identifier of the failed/retrying `WebhookLog` entry.

        Returns:
            WebhookLogResponse: The newly created retry attempt's outcome.

        Raises:
            NotFoundException: If no eligible (failed/retrying) log
                entry with that id exists.
            BusinessRuleException: If the parent webhook is disabled,
                soft-deleted, or has exceeded its configured
                `retry_count` for this delivery.
        """
        failed_log = await self.repository.get_failed_log_for_retry(log_id)
        if failed_log is None:
            raise NotFoundException(
                f"No failed or retrying delivery log '{log_id}' was found."
            )

        webhook = await self.repository.get_by_id(failed_log.webhook_id)
        if webhook is None:
            raise NotFoundException(
                f"Parent webhook for delivery log '{log_id}' was not found."
            )
        if not webhook.enabled:
            raise BusinessRuleException(
                f"Webhook '{webhook.id}' is disabled and cannot retry delivery."
            )
        if failed_log.attempt_count > webhook.retry_count:
            raise BusinessRuleException(
                f"Delivery log '{log_id}' has exhausted the configured "
                f"retry_count ({webhook.retry_count}) and must be handled "
                f"via the Dead Letter Queue."
            )

        payload = self._render_payload(webhook, source_payload=None)
        log = await self._dispatch(
            webhook,
            payload=payload,
            attempt_count=failed_log.attempt_count + 1,
        )
        await self.session.commit()
        return self._log_response(log)

    async def manual_trigger(
        self, webhook_id: uuid.UUID, event_payload: dict[str, Any]
    ) -> WebhookLogResponse:
        """Manually triggers a webhook delivery with a caller-supplied
        event payload, bypassing the normal domain-event dispatch path.

        Args:
            webhook_id: Identifier of the webhook to trigger.
            event_payload: The raw event payload to deliver (subject to
                `payload_template` shaping, if configured).

        Returns:
            WebhookLogResponse: The delivery attempt's outcome.

        Raises:
            NotFoundException: If no matching, non-deleted webhook exists.
            BusinessRuleException: If the webhook is disabled.
            ValidationException: If `event_payload` is not
                JSON-serializable.
        """
        webhook = await self._get_or_raise(webhook_id)
        if not webhook.enabled:
            raise BusinessRuleException(
                f"Webhook '{webhook_id}' is disabled and cannot be triggered."
            )

        try:
            json.dumps(event_payload)
        except (TypeError, ValueError) as exc:
            raise ValidationException(
                "event_payload must be JSON-serializable."
            ) from exc

        payload = self._render_payload(webhook, source_payload=event_payload)
        log = await self._dispatch(webhook, payload=payload, attempt_count=1)
        await self.session.commit()
        return self._log_response(log)

    async def test_webhook(self, webhook_id: uuid.UUID) -> WebhookLogResponse:
        """Sends a synthetic test delivery to a webhook's target URL.

        The test delivery uses a minimal, clearly-marked synthetic
        payload (`{"event": "test", ...}`) rather than any real domain
        data, and is logged like any other delivery attempt so its
        result is visible in delivery history/statistics.

        Args:
            webhook_id: Identifier of the webhook to test.

        Returns:
            WebhookLogResponse: The test delivery attempt's outcome.

        Raises:
            NotFoundException: If no matching, non-deleted webhook exists.
        """
        webhook = await self._get_or_raise(webhook_id)

        test_payload = {
            "event": "test",
            "webhook_id": str(webhook.id),
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "message": "This is a test delivery from the Webhook module.",
        }
        log = await self._dispatch(webhook, payload=test_payload, attempt_count=1)
        await self.session.commit()
        return self._log_response(log)

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------
    async def _get_or_raise(self, webhook_id: uuid.UUID) -> Webhook:
        """Fetches a non-deleted webhook or raises `NotFoundException`.

        Args:
            webhook_id: Identifier of the webhook to fetch.

        Returns:
            Webhook: The matching webhook ORM instance.

        Raises:
            NotFoundException: If no matching, non-deleted webhook exists.
        """
        webhook = await self.repository.get_by_id(webhook_id)
        if webhook is None:
            raise NotFoundException(f"Webhook '{webhook_id}' was not found.")
        return webhook

    @staticmethod
    def _render_payload(
        webhook: Webhook, *, source_payload: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        """Shapes the outbound delivery body from the source event payload.

        Args:
            webhook: The webhook whose `payload_template` (if any)
                governs the shaping.
            source_payload: The raw source event payload, or `None`
                (e.g. for retries with no new source data), in which
                case a minimal envelope is used.

        Returns:
            dict[str, Any]: The outbound request body. When no
            `payload_template` is configured, the raw payload (or a
            minimal envelope) is returned as-is, matching the
            `Webhook.payload_template` field's documented semantics.
        """
        base_payload = source_payload if source_payload is not None else {
            "event": webhook.event.value,
            "webhook_id": str(webhook.id),
        }
        if not webhook.payload_template:
            return base_payload
        # Simple, non-recursive field-mapping: template values that are
        # strings referencing a top-level source key are substituted;
        # all other template values are passed through literally.
        rendered: dict[str, Any] = {}
        for key, value in webhook.payload_template.items():
            if isinstance(value, str) and value in base_payload:
                rendered[key] = base_payload[value]
            else:
                rendered[key] = value
        return rendered

    def _build_auth_headers(
        self, webhook: Webhook, body_bytes: bytes
    ) -> dict[str, str]:
        """Builds the authentication/signing header(s) for a delivery attempt.

        Args:
            webhook: The webhook whose `authentication_type` and
                `secret_key` govern signing.
            body_bytes: The exact serialized outbound request body,
                used for HMAC signing.

        Returns:
            dict[str, str]: Headers to merge into the outbound request.
        """
        if webhook.authentication_type == AuthenticationType.NONE:
            return {}
        if webhook.authentication_type == AuthenticationType.HMAC_SIGNATURE:
            signature = hmac.new(
                (webhook.secret_key or "").encode("utf-8"),
                body_bytes,
                hashlib.sha256,
            ).hexdigest()
            return {"X-Webhook-Signature": f"sha256={signature}"}
        if webhook.authentication_type == AuthenticationType.BEARER_TOKEN:
            return {"Authorization": f"Bearer {webhook.secret_key}"}
        if webhook.authentication_type == AuthenticationType.API_KEY:
            return {"X-API-Key": webhook.secret_key or ""}
        if webhook.authentication_type == AuthenticationType.BASIC_AUTH:
            import base64

            token = base64.b64encode(
                (webhook.secret_key or "").encode("utf-8")
            ).decode("ascii")
            return {"Authorization": f"Basic {token}"}
        return {}

    async def _dispatch(
        self,
        webhook: Webhook,
        *,
        payload: dict[str, Any],
        attempt_count: int,
    ) -> WebhookLog:
        """Performs a single outbound HTTP delivery attempt and logs it.

        This is the single low-level delivery primitive shared by
        `manual_trigger`, `test_webhook`, and `retry_delivery`. It never
        raises on delivery failure (timeouts, connection errors,
        non-2xx responses) -- those are recorded as a `FAILED` (or
        `DEAD_LETTERED`, once `retry_count` is exhausted) `WebhookLog`
        row and returned normally, since a downstream delivery failure
        is expected operational behavior, not a service-layer error.

        Args:
            webhook: The webhook being delivered to.
            payload: The already-shaped outbound request body.
            attempt_count: The 1-indexed attempt number this delivery
                represents.

        Returns:
            WebhookLog: The persisted (but not yet committed) log row
            describing this delivery attempt's outcome.
        """
        import httpx

        body_bytes = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        headers.update(webhook.custom_headers or {})
        headers.update(self._build_auth_headers(webhook, body_bytes))

        started_at = time.monotonic()
        delivery_status: DeliveryStatus
        response_code: Optional[int] = None
        response_body: Optional[str] = None
        error_message: Optional[str] = None

        try:
            async with httpx.AsyncClient(timeout=webhook.timeout_seconds) as client:
                response = await client.request(
                    webhook.http_method,
                    webhook.target_url,
                    content=body_bytes,
                    headers=headers,
                )
            response_code = response.status_code
            response_body = response.text[:8000] if response.text else None
            if 200 <= response.status_code < 300:
                delivery_status = DeliveryStatus.SUCCESS
            else:
                delivery_status = (
                    DeliveryStatus.DEAD_LETTERED
                    if attempt_count >= webhook.retry_count
                    else DeliveryStatus.FAILED
                )
                error_message = f"Non-2xx response: HTTP {response.status_code}"
        except httpx.TimeoutException as exc:
            delivery_status = (
                DeliveryStatus.DEAD_LETTERED
                if attempt_count >= webhook.retry_count
                else DeliveryStatus.FAILED
            )
            error_message = f"Delivery timed out after {webhook.timeout_seconds}s: {exc}"
        except httpx.HTTPError as exc:
            delivery_status = (
                DeliveryStatus.DEAD_LETTERED
                if attempt_count >= webhook.retry_count
                else DeliveryStatus.FAILED
            )
            error_message = f"Delivery transport error: {exc}"

        duration_ms = round((time.monotonic() - started_at) * 1000, 3)
        now = datetime.now(timezone.utc)

        log = await self.repository.create_log(
            {
                "webhook_id": webhook.id,
                "delivery_status": delivery_status,
                "response_code": response_code,
                "response_body": response_body,
                "attempt_count": attempt_count,
                "duration_ms": duration_ms,
                "error_message": error_message,
                "delivered_at": now,
            }
        )

        await self.repository.record_delivery_outcome(
            webhook,
            succeeded=delivery_status == DeliveryStatus.SUCCESS,
            occurred_at=now,
        )

        return log

    @staticmethod
    def _to_response(webhook: Webhook) -> WebhookResponse:
        """Converts a `Webhook` ORM instance into its API response schema.

        Args:
            webhook: The ORM instance to serialize.

        Returns:
            WebhookResponse: The response schema, with the derived
            `has_secret_key` flag set explicitly (per
            `WebhookResponse.has_secret_key`'s documented contract,
            since it cannot be derived via `from_attributes` alone).
        """
        response = WebhookResponse.model_validate(webhook)
        return response.model_copy(
            update={"has_secret_key": webhook.secret_key is not None}
        )