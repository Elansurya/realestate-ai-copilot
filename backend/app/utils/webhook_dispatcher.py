"""
backend/app/utils/webhook_dispatcher.py

Reusable outbound delivery dispatcher for the Enterprise Webhook
module of the Enterprise Real Estate AI Copilot CRM.

This module is the single canonical location for the module's
delivery *mechanics* -- as distinct from `app.utils.webhook_validator`
(business-rule validation) and `app.repositories.webhook_repository`
(persistence). It is intentionally framework-agnostic (no FastAPI
imports) so it can be invoked from:
    - `app.services.webhook_service.WebhookService` (interactive
      test/retry/manual-trigger delivery, request-response cycle),
    - a background worker / task queue consumer that dispatches
      webhooks in reaction to real domain events (e.g. `lead_created`)
      published elsewhere in the application, outside the request
      cycle covered by this phase.

Building blocks (each independently reusable/testable):
    - `EventDispatcher.register` / `EventDispatcher.dispatch_event`
        -- "Register Dispatcher" / "Event Dispatcher": an in-process
        registry mapping a `WebhookEvent` to observer callables that
        should be notified (in addition to the webhooks themselves)
        whenever that event is dispatched -- e.g. for cross-cutting
        concerns like metrics or audit logging.
    - `PayloadBuilder.build`
        -- shapes the outbound request body from a source event
        payload and the webhook's `payload_template`.
    - `HttpSender.send`
        -- performs a single outbound HTTP delivery attempt (pure
        network I/O, no DB access, no retry logic).
    - `RetryHandler`
        -- decides whether a failed attempt is retryable and computes
        exponential backoff delay.
    - `DeadLetterQueueHandler`
        -- decides whether an exhausted delivery should be routed to
        the Dead Letter Queue and builds the terminal log payload.
    - `DeliveryLogger`
        -- persists a `WebhookLog` row (via the injected
        `WebhookRepository`) and updates the parent webhook's rollup
        delivery timestamps.
    - `StatisticsCollector`
        -- lightweight in-process delivery counters (fast, real-time
        observability) plus a pass-through to the repository's
        durable, DB-backed aggregate statistics.
    - `WebhookDispatcher`
        -- the top-level façade composing all of the above into a
        single `dispatch()` entry point.

Raises ONLY the project's domain exceptions (never
`fastapi.HTTPException`), matching the convention already used by
`app.services.webhook_service`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from app.core.exceptions import BusinessRuleException
from app.models.webhook import AuthenticationType, DeliveryStatus, Webhook, WebhookEvent
from app.repositories.webhook_repository import WebhookRepository
from app.utils.webhook_validator import WebhookValidator

__all__ = [
    "DeliveryResult",
    "EventDispatcher",
    "PayloadBuilder",
    "HttpSender",
    "RetryHandler",
    "DeadLetterQueueHandler",
    "DeliveryLogger",
    "StatisticsCollector",
    "WebhookDispatcher",
]

#: Signature for an event-registered observer callback: invoked with
#: the event and the raw source payload after every dispatch attempt
#: for that event, in addition to the webhook(s) actually delivered to.
EventHandler = Callable[[WebhookEvent, dict[str, Any]], Awaitable[None]]

_DEFAULT_BASE_BACKOFF_SECONDS: float = 2.0
_DEFAULT_MAX_BACKOFF_SECONDS: float = 300.0


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of a single outbound HTTP delivery attempt.

    Attributes:
        success: Whether the attempt received a 2xx response.
        response_code: HTTP status code returned, if a response was received.
        response_body: Raw response body (truncated), if any.
        error_message: Human-readable failure detail, if the attempt failed.
        duration_ms: Wall-clock duration of the attempt, in milliseconds.
    """

    success: bool
    response_code: Optional[int]
    response_body: Optional[str]
    error_message: Optional[str]
    duration_ms: float


# ---------------------------------------------------------------------------
# Register Dispatcher / Event Dispatcher
# ---------------------------------------------------------------------------
class EventDispatcher:
    """In-process registry of observer callbacks per `WebhookEvent`.

    This complements (does not replace) actual webhook delivery: it
    lets other parts of the application "register" interest in a
    domain event being dispatched through the Webhook module, without
    coupling this module to those callers.
    """

    def __init__(self) -> None:
        """Initializes an empty per-event handler registry."""
        self._handlers: dict[WebhookEvent, list[EventHandler]] = defaultdict(list)

    def register(self, event: WebhookEvent, handler: EventHandler) -> None:
        """Registers an observer callback for a domain event.

        Args:
            event: The domain event to observe.
            handler: An async callable invoked with `(event, payload)`
                every time that event is dispatched.

        Raises:
            ValidationException: If `event` is not a recognized
                `WebhookEvent` member.
        """
        WebhookValidator.validate_event(event)
        self._handlers[event].append(handler)

    def unregister(self, event: WebhookEvent, handler: EventHandler) -> None:
        """Removes a previously registered observer callback, if present.

        Args:
            event: The domain event the handler was registered against.
            handler: The exact callable instance to remove.
        """
        handlers = self._handlers.get(event)
        if handlers and handler in handlers:
            handlers.remove(handler)

    async def notify(self, event: WebhookEvent, payload: dict[str, Any]) -> None:
        """Invokes every observer callback registered for `event`.

        Handler exceptions are swallowed (and should be logged by the
        handler itself) so that one misbehaving observer cannot abort
        webhook delivery to the rest.

        Args:
            event: The domain event being dispatched.
            payload: The raw source event payload.
        """
        for handler in self._handlers.get(event, []):
            try:
                await handler(event, payload)
            except Exception:  # noqa: BLE001 - observers must never break delivery
                continue


# ---------------------------------------------------------------------------
# Payload Builder
# ---------------------------------------------------------------------------
class PayloadBuilder:
    """Shapes outbound request bodies from source event payloads."""

    @staticmethod
    def build(webhook: Webhook, source_payload: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Builds the outbound delivery body for a webhook.

        Args:
            webhook: The webhook whose `payload_template` (if any)
                governs the shaping.
            source_payload: The raw source event payload, or `None`
                (e.g. for retries with no new source data), in which
                case a minimal envelope is used.

        Returns:
            dict[str, Any]: The outbound request body. When no
            `payload_template` is configured, the raw payload (or a
            minimal envelope) is returned as-is, matching
            `Webhook.payload_template`'s documented semantics.
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

    @staticmethod
    def build_headers(webhook: Webhook, body_bytes: bytes) -> dict[str, str]:
        """Builds the full outbound header set (content-type + auth/signing
        headers + static custom headers) for a delivery attempt.

        Args:
            webhook: The webhook whose `authentication_type`,
                `secret_key`, and `custom_headers` govern the headers.
            body_bytes: The exact serialized outbound request body,
                used for HMAC signing.

        Returns:
            dict[str, str]: The complete header set for the request.
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        headers.update(webhook.custom_headers or {})
        headers.update(PayloadBuilder._auth_headers(webhook, body_bytes))
        return headers

    @staticmethod
    def _auth_headers(webhook: Webhook, body_bytes: bytes) -> dict[str, str]:
        """Builds only the authentication/signing header(s).

        Args:
            webhook: The webhook whose `authentication_type` and
                `secret_key` govern signing.
            body_bytes: The exact serialized outbound request body,
                used for HMAC signing.

        Returns:
            dict[str, str]: Authentication/signing headers to merge
            into the outbound request.
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
            token = base64.b64encode((webhook.secret_key or "").encode("utf-8")).decode(
                "ascii"
            )
            return {"Authorization": f"Basic {token}"}
        return {}


# ---------------------------------------------------------------------------
# HTTP Sender
# ---------------------------------------------------------------------------
class HttpSender:
    """Performs a single outbound HTTP delivery attempt (pure network I/O)."""

    @staticmethod
    async def send(webhook: Webhook, payload: dict[str, Any]) -> DeliveryResult:
        """Sends one HTTP request to a webhook's target URL.

        Args:
            webhook: The webhook being delivered to (`target_url`,
                `http_method`, and `timeout_seconds` govern the request).
            payload: The already-shaped outbound request body.

        Returns:
            DeliveryResult: The outcome of this single attempt. Network
            failures (timeouts, connection errors) are captured in the
            result rather than raised, so callers can uniformly log
            both HTTP-level and transport-level failures.
        """
        import httpx

        body_bytes = json.dumps(payload).encode("utf-8")
        headers = PayloadBuilder.build_headers(webhook, body_bytes)

        started_at = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=webhook.timeout_seconds) as client:
                response = await client.request(
                    webhook.http_method,
                    webhook.target_url,
                    content=body_bytes,
                    headers=headers,
                )
            duration_ms = round((time.monotonic() - started_at) * 1000, 3)
            success = 200 <= response.status_code < 300
            return DeliveryResult(
                success=success,
                response_code=response.status_code,
                response_body=response.text[:8000] if response.text else None,
                error_message=None
                if success
                else f"Non-2xx response: HTTP {response.status_code}",
                duration_ms=duration_ms,
            )
        except httpx.TimeoutException as exc:
            duration_ms = round((time.monotonic() - started_at) * 1000, 3)
            return DeliveryResult(
                success=False,
                response_code=None,
                response_body=None,
                error_message=f"Delivery timed out after {webhook.timeout_seconds}s: {exc}",
                duration_ms=duration_ms,
            )
        except httpx.HTTPError as exc:
            duration_ms = round((time.monotonic() - started_at) * 1000, 3)
            return DeliveryResult(
                success=False,
                response_code=None,
                response_body=None,
                error_message=f"Delivery transport error: {exc}",
                duration_ms=duration_ms,
            )


# ---------------------------------------------------------------------------
# Retry Handler
# ---------------------------------------------------------------------------
class RetryHandler:
    """Retry-eligibility and exponential-backoff computation."""

    def __init__(
        self,
        *,
        base_backoff_seconds: float = _DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: float = _DEFAULT_MAX_BACKOFF_SECONDS,
    ) -> None:
        """Initializes the retry/backoff policy.

        Args:
            base_backoff_seconds: The backoff delay used for the first
                retry; each subsequent retry doubles this value.
            max_backoff_seconds: The maximum backoff delay allowed,
                regardless of attempt number.
        """
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds

    def should_retry(self, webhook: Webhook, attempt_count: int, succeeded: bool) -> bool:
        """Determines whether another delivery attempt should be scheduled.

        Args:
            webhook: The webhook the attempt was made for (its
                `retry_count` is the policy ceiling).
            attempt_count: The 1-indexed attempt number that just completed.
            succeeded: Whether that attempt succeeded.

        Returns:
            bool: `True` if a retry should be scheduled; `False` if the
            attempt succeeded or the retry policy has been exhausted.
        """
        if succeeded:
            return False
        if not webhook.enabled:
            return False
        return attempt_count < webhook.retry_count

    def compute_backoff_seconds(self, attempt_count: int) -> float:
        """Computes the exponential backoff delay before the next attempt.

        Args:
            attempt_count: The 1-indexed attempt number that just failed.

        Returns:
            float: The delay, in seconds, before the next attempt
            should be made, capped at `max_backoff_seconds`.
        """
        delay = self.base_backoff_seconds * (2 ** max(attempt_count - 1, 0))
        return min(delay, self.max_backoff_seconds)


# ---------------------------------------------------------------------------
# Dead Letter Queue Handler
# ---------------------------------------------------------------------------
class DeadLetterQueueHandler:
    """Decides terminal delivery-status classification once retries are
    exhausted, and prepares the corresponding log payload."""

    @staticmethod
    def is_exhausted(webhook: Webhook, attempt_count: int) -> bool:
        """Determines whether a failed attempt has exhausted the retry policy.

        Args:
            webhook: The webhook the attempt was made for.
            attempt_count: The 1-indexed attempt number that just failed.

        Returns:
            bool: `True` if `attempt_count` has reached or exceeded
            `webhook.retry_count`, meaning no further retries should
            be attempted and the delivery should be dead-lettered.
        """
        return attempt_count >= webhook.retry_count

    @staticmethod
    def classify(
        webhook: Webhook, attempt_count: int, result: DeliveryResult
    ) -> DeliveryStatus:
        """Classifies a delivery attempt's terminal `DeliveryStatus`.

        Args:
            webhook: The webhook the attempt was made for.
            attempt_count: The 1-indexed attempt number this result
                represents.
            result: The outcome of the HTTP delivery attempt.

        Returns:
            DeliveryStatus: `SUCCESS` if the attempt succeeded;
            `DEAD_LETTERED` if it failed and the retry policy is
            exhausted; otherwise `FAILED` (eligible for retry).
        """
        if result.success:
            return DeliveryStatus.SUCCESS
        if DeadLetterQueueHandler.is_exhausted(webhook, attempt_count):
            return DeliveryStatus.DEAD_LETTERED
        return DeliveryStatus.FAILED


# ---------------------------------------------------------------------------
# Delivery Logger
# ---------------------------------------------------------------------------
class DeliveryLogger:
    """Persists delivery attempts and updates parent-webhook rollup state.

    Attributes:
        repository: The `WebhookRepository` used to persist log rows
            and rollup timestamps. The caller (Service layer) retains
            ownership of the transaction boundary (`commit()`); this
            class only `flush()`es, via the repository.
    """

    def __init__(self, repository: WebhookRepository) -> None:
        """Initializes the logger with an injected repository.

        Args:
            repository: The repository used for persistence.
        """
        self.repository = repository

    async def log_attempt(
        self,
        webhook: Webhook,
        *,
        attempt_count: int,
        status: DeliveryStatus,
        result: DeliveryResult,
    ):
        """Persists a single delivery attempt and updates the parent
        webhook's rollup delivery-state timestamps.

        Args:
            webhook: The webhook the attempt was made for.
            attempt_count: The 1-indexed attempt number this row represents.
            status: The classified terminal `DeliveryStatus` for this attempt.
            result: The raw HTTP delivery outcome being logged.

        Returns:
            WebhookLog: The persisted (flushed, not committed) log row.
        """
        now = datetime.now(timezone.utc)
        log = await self.repository.create_log(
            {
                "webhook_id": webhook.id,
                "delivery_status": status,
                "response_code": result.response_code,
                "response_body": result.response_body,
                "attempt_count": attempt_count,
                "duration_ms": result.duration_ms,
                "error_message": result.error_message,
                "delivered_at": now,
            }
        )
        await self.repository.record_delivery_outcome(
            webhook, succeeded=status == DeliveryStatus.SUCCESS, occurred_at=now
        )
        return log


# ---------------------------------------------------------------------------
# Statistics Collector
# ---------------------------------------------------------------------------
class StatisticsCollector:
    """Lightweight in-process delivery counters, plus a pass-through to
    the repository's durable, DB-backed aggregate statistics.

    The in-process counters are process-local and reset on restart --
    useful for cheap, real-time dashboards/health checks without a DB
    round-trip. For durable, historically-accurate reporting, use
    `get_persisted_statistics`, which is backed by `WebhookLog` rows.
    """

    def __init__(self) -> None:
        """Initializes empty in-process delivery counters."""
        self._counts: Counter[str] = Counter()

    def record(self, status: DeliveryStatus) -> None:
        """Increments the in-process counter for a delivery status.

        Args:
            status: The `DeliveryStatus` just recorded for a delivery attempt.
        """
        self._counts[status.value] += 1

    def snapshot(self) -> dict[str, int]:
        """Returns a point-in-time copy of the in-process counters.

        Returns:
            dict[str, int]: Mapping of `DeliveryStatus` value -> count,
            since process start (or the last `reset()`).
        """
        return dict(self._counts)

    def reset(self) -> None:
        """Clears all in-process counters."""
        self._counts.clear()

    @staticmethod
    async def get_persisted_statistics(
        repository: WebhookRepository, *, webhook_id: Optional[Any] = None
    ) -> dict[str, Any]:
        """Fetches durable, DB-backed aggregate statistics.

        Args:
            repository: The repository to query.
            webhook_id: Optional webhook UUID to scope statistics to a
                single webhook; `None` computes statistics across all
                (non-deleted) webhooks.

        Returns:
            dict[str, Any]: The raw aggregate statistics, as returned
            by `WebhookRepository.get_statistics`.
        """
        return await repository.get_statistics(webhook_id=webhook_id)


# ---------------------------------------------------------------------------
# Top-Level Façade
# ---------------------------------------------------------------------------
@dataclass
class WebhookDispatcher:
    """Composes payload building, HTTP sending, retry/backoff, DLQ
    classification, delivery logging, and statistics collection into a
    single delivery entry point.

    Attributes:
        repository: The `WebhookRepository` used for logging and
            statistics.
        events: The `EventDispatcher` used to notify registered
            observers alongside actual webhook delivery.
        retry_handler: The retry-eligibility/backoff policy in effect.
        statistics: In-process delivery counters.
    """

    repository: WebhookRepository
    events: EventDispatcher = field(default_factory=EventDispatcher)
    retry_handler: RetryHandler = field(default_factory=RetryHandler)
    statistics: StatisticsCollector = field(default_factory=StatisticsCollector)

    async def dispatch(
        self,
        webhook: Webhook,
        *,
        source_payload: Optional[dict[str, Any]],
        attempt_count: int = 1,
    ):
        """Performs one full delivery attempt: build payload, send,
        classify outcome, log it, and update in-process statistics.

        This does NOT loop through retries itself -- each call performs
        exactly one HTTP attempt for the given `attempt_count`. Callers
        driving an automatic retry loop (e.g. a background worker)
        should use `retry_handler.should_retry` /
        `retry_handler.compute_backoff_seconds` to decide whether and
        when to call `dispatch` again with `attempt_count + 1`. This
        mirrors how `retry_delivery` (an explicit, user-triggered
        single retry) is exposed at the Service/API layer.

        Args:
            webhook: The webhook to deliver to. Must be `enabled`.
            source_payload: The raw source event payload, or `None`
                to use a minimal envelope (e.g. for retries with no
                new source data).
            attempt_count: The 1-indexed attempt number this delivery
                represents.

        Returns:
            WebhookLog: The persisted (flushed, not committed) log row
            describing this delivery attempt's outcome.

        Raises:
            BusinessRuleException: If `webhook.enabled` is `False`.
        """
        if not webhook.enabled:
            raise BusinessRuleException(
                f"Webhook '{webhook.id}' is disabled and cannot be dispatched."
            )

        payload = PayloadBuilder.build(webhook, source_payload)
        result = await HttpSender.send(webhook, payload)
        status = DeadLetterQueueHandler.classify(webhook, attempt_count, result)

        logger = DeliveryLogger(self.repository)
        log = await logger.log_attempt(
            webhook, attempt_count=attempt_count, status=status, result=result
        )

        self.statistics.record(status)
        await self.events.notify(
            webhook.event, source_payload or {"webhook_id": str(webhook.id)}
        )

        return log

    async def dispatch_event(
        self,
        event: WebhookEvent,
        source_payload: dict[str, Any],
        webhooks: list[Webhook],
    ) -> list:
        """Dispatches a domain event to every eligible subscribed webhook.

        Args:
            event: The domain event that occurred.
            source_payload: The raw source event payload to deliver.
            webhooks: Candidate webhooks to consider (typically
                pre-filtered by the caller to `event`-matching,
                `enabled=True`, non-deleted records via the
                Repository/Service layer).

        Returns:
            list[WebhookLog]: One persisted log row per webhook
            actually dispatched to (webhooks that don't match `event`
            or are disabled are skipped, not errored).
        """
        WebhookValidator.validate_event(event)
        logs = []
        for webhook in webhooks:
            if webhook.event != event or not webhook.enabled or webhook.is_deleted:
                continue
            logs.append(
                await self.dispatch(webhook, source_payload=source_payload, attempt_count=1)
            )
        return logs