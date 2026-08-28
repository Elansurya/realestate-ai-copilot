"""
backend/app/utils/health_checker.py

Low-level, dependency-free probe utilities for the Enterprise Monitoring
& Health module of the Enterprise Real Estate AI Copilot CRM.

This module contains ONLY the mechanics of *actually reaching out* to a
live dependency (database, object storage, an AI provider, an external
integration) and timing/measuring the outcome. It knows nothing about
`SystemHealth` rows, persistence, or business rules -- it simply returns
plain, JSON-serializable result dicts that the API layer feeds into
`MonitoringService.execute_health_check()`, which is the single source
of truth for turning raw metrics into a `HealthStatus`.

Design notes:
    - Every probe function is defensive: it never raises for an
      *expected* failure mode (timeout, connection refused, DNS
      failure, HTTP error status). It catches these and reports them
      as a failed probe result instead, because a health checker that
      itself crashes on an unhealthy dependency defeats the purpose of
      health checking.
    - Every probe function returns the same result shape so callers can
      treat all component types uniformly:
        {
            "is_healthy": bool,
            "response_time_ms": float,
            "error_count": int,       # 1 if this single probe failed, else 0
            "message": Optional[str], # human-readable detail / error
            "meta_data": Optional[dict],
        }
    - Probe functions accept plain connection parameters rather than
      framework objects (e.g. an already-open `AsyncSession` for the
      database probe) so this module has no import-time dependency on
      the repository/service layer and stays trivially unit-testable.
    - Timeouts are always enforced explicitly (`asyncio.wait_for` /
      client-level timeouts) so a hung dependency can never hang a
      health-check request.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

__all__ = [
    "ProbeResult",
    "check_database_health",
    "check_storage_health",
    "check_ai_provider_health",
    "check_integration_health",
    "check_notification_service_health",
    "check_search_engine_health",
    "check_workflow_engine_health",
]

# --------------------------------------------------------------------------
# Probe Configuration
# --------------------------------------------------------------------------
_DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0

ProbeResult = dict[str, Any]


def _elapsed_ms(started_at: float) -> float:
    """
    Computes elapsed time in milliseconds since a `time.perf_counter()`
    reading.

    Args:
        started_at: The `time.perf_counter()` value captured before the
            probe began.

    Returns:
        Elapsed time in milliseconds, rounded to 3 decimal places.
    """
    return round((time.perf_counter() - started_at) * 1000, 3)


def _success_result(
    response_time_ms: float,
    message: Optional[str] = None,
    meta_data: Optional[dict] = None,
) -> ProbeResult:
    """Builds a successful probe result dict."""
    return {
        "is_healthy": True,
        "response_time_ms": response_time_ms,
        "error_count": 0,
        "message": message,
        "meta_data": meta_data,
    }


def _failure_result(
    response_time_ms: float,
    message: str,
    meta_data: Optional[dict] = None,
) -> ProbeResult:
    """Builds a failed probe result dict."""
    return {
        "is_healthy": False,
        "response_time_ms": response_time_ms,
        "error_count": 1,
        "message": message,
        "meta_data": meta_data,
    }


# --------------------------------------------------------------------------
# Database Health Probe
# --------------------------------------------------------------------------
async def check_database_health(
    session: Any,
    *,
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """
    Probes database connectivity/liveness by executing a trivial
    `SELECT 1` against the already-open async session.

    Args:
        session: An open `sqlalchemy.ext.asyncio.AsyncSession` (or any
            object exposing an async `.execute()` compatible with
            SQLAlchemy Core `text()` constructs).
        timeout_seconds: Maximum time to wait for the probe query.

    Returns:
        A `ProbeResult` dict describing the outcome.
    """
    from sqlalchemy import text

    started_at = time.perf_counter()
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return _failure_result(
            _elapsed_ms(started_at),
            f"Database probe timed out after {timeout_seconds}s.",
        )
    except Exception as exc:  # noqa: BLE001 - any driver/connection error is a probe failure
        return _failure_result(_elapsed_ms(started_at), f"Database probe failed: {exc}")

    return _success_result(_elapsed_ms(started_at), "Database connection established and query executed successfully.")


# --------------------------------------------------------------------------
# Storage Health Probe
# --------------------------------------------------------------------------
async def check_storage_health(
    *,
    storage_client: Optional[Any] = None,
    bucket_name: Optional[str] = None,
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """
    Probes object/file storage availability (e.g. AWS S3, Azure Blob,
    GCP Storage) by issuing a lightweight existence/metadata check
    against the configured bucket/container.

    Args:
        storage_client: An initialized storage SDK client exposing an
            async or sync `head_bucket` / `get_bucket_metadata`-style
            call. If `None`, the probe reports UNKNOWN-style failure
            since no client was configured.
        bucket_name: The bucket/container name to check.
        timeout_seconds: Maximum time to wait for the probe.

    Returns:
        A `ProbeResult` dict describing the outcome.
    """
    started_at = time.perf_counter()

    if storage_client is None or bucket_name is None:
        return _failure_result(
            _elapsed_ms(started_at),
            "Storage client or bucket is not configured for this environment.",
        )

    try:
        async def _probe() -> None:
            head = getattr(storage_client, "head_bucket", None)
            if head is None:
                raise RuntimeError("Storage client does not expose a head_bucket check.")
            result = head(Bucket=bucket_name) if "Bucket" in head.__code__.co_varnames else head(bucket_name)
            if asyncio.iscoroutine(result):
                await result

        await asyncio.wait_for(_probe(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return _failure_result(
            _elapsed_ms(started_at),
            f"Storage probe timed out after {timeout_seconds}s.",
        )
    except Exception as exc:  # noqa: BLE001
        return _failure_result(_elapsed_ms(started_at), f"Storage probe failed: {exc}")

    return _success_result(
        _elapsed_ms(started_at),
        f"Storage bucket '{bucket_name}' is reachable.",
        meta_data={"bucket_name": bucket_name},
    )


# --------------------------------------------------------------------------
# AI Provider Health Probe
# --------------------------------------------------------------------------
async def check_ai_provider_health(
    provider_name: str,
    *,
    ping_callable: Optional[Any] = None,
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """
    Probes an external AI/LLM provider (e.g. OpenAI, Anthropic) for
    reachability. The actual network call is delegated to
    `ping_callable` (typically a thin wrapper around the provider SDK's
    lightweight "list models" or health-check endpoint) so this module
    never hard-codes a dependency on any specific AI SDK.

    Args:
        provider_name: Human-readable name of the provider being probed
            (e.g. "anthropic", "openai").
        ping_callable: An async, zero-argument callable that performs
            the actual lightweight round-trip and raises on failure. If
            `None`, the probe reports a configuration failure.
        timeout_seconds: Maximum time to wait for the probe.

    Returns:
        A `ProbeResult` dict describing the outcome.
    """
    started_at = time.perf_counter()

    if ping_callable is None:
        return _failure_result(
            _elapsed_ms(started_at),
            f"No health-check callable configured for AI provider '{provider_name}'.",
        )

    try:
        await asyncio.wait_for(ping_callable(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return _failure_result(
            _elapsed_ms(started_at),
            f"AI provider '{provider_name}' probe timed out after {timeout_seconds}s.",
        )
    except Exception as exc:  # noqa: BLE001
        return _failure_result(
            _elapsed_ms(started_at),
            f"AI provider '{provider_name}' probe failed: {exc}",
        )

    return _success_result(
        _elapsed_ms(started_at),
        f"AI provider '{provider_name}' is reachable.",
        meta_data={"provider_name": provider_name},
    )


# --------------------------------------------------------------------------
# External Integration Health Probe
# --------------------------------------------------------------------------
async def check_integration_health(
    integration_name: str,
    *,
    endpoint_url: Optional[str] = None,
    http_client: Optional[Any] = None,
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """
    Probes a configured third-party integration (payment gateway,
    calendar provider, CRM webhook target, etc.) via a lightweight HTTP
    GET/HEAD against its configured health/status endpoint.

    Args:
        integration_name: Human-readable name of the integration.
        endpoint_url: The health/status URL to probe.
        http_client: An `httpx.AsyncClient`-compatible object exposing
            an async `.get(url, timeout=...)` method. If `None` or
            `endpoint_url` is `None`, the probe reports a configuration
            failure.
        timeout_seconds: Maximum time to wait for the probe.

    Returns:
        A `ProbeResult` dict describing the outcome.
    """
    started_at = time.perf_counter()

    if http_client is None or endpoint_url is None:
        return _failure_result(
            _elapsed_ms(started_at),
            f"No endpoint or HTTP client configured for integration '{integration_name}'.",
        )

    try:
        response = await asyncio.wait_for(
            http_client.get(endpoint_url, timeout=timeout_seconds), timeout=timeout_seconds
        )
        status_code = getattr(response, "status_code", None)
        if status_code is None or status_code >= 400:
            return _failure_result(
                _elapsed_ms(started_at),
                f"Integration '{integration_name}' returned status {status_code}.",
                meta_data={"endpoint_url": endpoint_url, "status_code": status_code},
            )
    except asyncio.TimeoutError:
        return _failure_result(
            _elapsed_ms(started_at),
            f"Integration '{integration_name}' probe timed out after {timeout_seconds}s.",
        )
    except Exception as exc:  # noqa: BLE001
        return _failure_result(
            _elapsed_ms(started_at),
            f"Integration '{integration_name}' probe failed: {exc}",
        )

    return _success_result(
        _elapsed_ms(started_at),
        f"Integration '{integration_name}' is reachable.",
        meta_data={"endpoint_url": endpoint_url},
    )


# --------------------------------------------------------------------------
# Notification Service Health Probe
# --------------------------------------------------------------------------
async def check_notification_service_health(
    channel_name: str,
    *,
    ping_callable: Optional[Any] = None,
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """
    Probes an outbound notification channel (email, SMS, WhatsApp,
    push) for reachability, delegating the actual provider round-trip
    to `ping_callable`.

    Args:
        channel_name: Human-readable name of the notification channel
            (e.g. "sendgrid-email", "twilio-sms").
        ping_callable: An async, zero-argument callable performing the
            round-trip and raising on failure.
        timeout_seconds: Maximum time to wait for the probe.

    Returns:
        A `ProbeResult` dict describing the outcome.
    """
    started_at = time.perf_counter()

    if ping_callable is None:
        return _failure_result(
            _elapsed_ms(started_at),
            f"No health-check callable configured for notification channel '{channel_name}'.",
        )

    try:
        await asyncio.wait_for(ping_callable(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return _failure_result(
            _elapsed_ms(started_at),
            f"Notification channel '{channel_name}' probe timed out after {timeout_seconds}s.",
        )
    except Exception as exc:  # noqa: BLE001
        return _failure_result(
            _elapsed_ms(started_at),
            f"Notification channel '{channel_name}' probe failed: {exc}",
        )

    return _success_result(
        _elapsed_ms(started_at),
        f"Notification channel '{channel_name}' is reachable.",
        meta_data={"channel_name": channel_name},
    )


# --------------------------------------------------------------------------
# Search Engine Health Probe
# --------------------------------------------------------------------------
async def check_search_engine_health(
    *,
    search_client: Optional[Any] = None,
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """
    Probes the search/indexing subsystem (e.g. Elasticsearch,
    OpenSearch, a managed search API) via a lightweight cluster-health
    or ping-style call.

    Args:
        search_client: A client exposing an async or sync `ping()` /
            `cluster_health()`-style method. If `None`, the probe
            reports a configuration failure.
        timeout_seconds: Maximum time to wait for the probe.

    Returns:
        A `ProbeResult` dict describing the outcome.
    """
    started_at = time.perf_counter()

    if search_client is None:
        return _failure_result(
            _elapsed_ms(started_at),
            "No search engine client configured for this environment.",
        )

    try:
        async def _probe() -> None:
            ping = getattr(search_client, "ping", None)
            if ping is None:
                raise RuntimeError("Search client does not expose a ping check.")
            result = ping()
            if asyncio.iscoroutine(result):
                await result

        await asyncio.wait_for(_probe(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return _failure_result(
            _elapsed_ms(started_at),
            f"Search engine probe timed out after {timeout_seconds}s.",
        )
    except Exception as exc:  # noqa: BLE001
        return _failure_result(_elapsed_ms(started_at), f"Search engine probe failed: {exc}")

    return _success_result(_elapsed_ms(started_at), "Search engine cluster is reachable.")


# --------------------------------------------------------------------------
# Workflow Engine Health Probe
# --------------------------------------------------------------------------
async def check_workflow_engine_health(
    *,
    queue_client: Optional[Any] = None,
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """
    Probes the workflow/automation execution subsystem, typically by
    checking that its backing task queue / broker is reachable.

    Args:
        queue_client: A client exposing an async or sync connectivity
            check (e.g. a broker `ping()`). If `None`, the probe
            reports a configuration failure.
        timeout_seconds: Maximum time to wait for the probe.

    Returns:
        A `ProbeResult` dict describing the outcome.
    """
    started_at = time.perf_counter()

    if queue_client is None:
        return _failure_result(
            _elapsed_ms(started_at),
            "No workflow queue/broker client configured for this environment.",
        )

    try:
        async def _probe() -> None:
            ping = getattr(queue_client, "ping", None)
            if ping is None:
                raise RuntimeError("Queue client does not expose a ping check.")
            result = ping()
            if asyncio.iscoroutine(result):
                await result

        await asyncio.wait_for(_probe(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return _failure_result(
            _elapsed_ms(started_at),
            f"Workflow engine probe timed out after {timeout_seconds}s.",
        )
    except Exception as exc:  # noqa: BLE001
        return _failure_result(_elapsed_ms(started_at), f"Workflow engine probe failed: {exc}")

    return _success_result(_elapsed_ms(started_at), "Workflow engine broker is reachable.")