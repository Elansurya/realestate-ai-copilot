"""
backend/app/core/exceptions.py

Centralized global exception handling for the Real Estate AI Copilot
CRM API.

Responsibilities:
    - Provide a centralized, reusable `AppException` hierarchy
      (`ValidationException`, `NotFoundException`, `ConflictException`,
      etc.) that new services can raise directly instead of each
      defining bespoke exception classes and handlers, the way the
      Lead domain currently does.
    - Translate every domain exception raised by `LeadService` into a
      consistent, structured JSON error response.
    - Translate framework-level and infrastructure-level exceptions
      (request validation, HTTP exceptions, SQLAlchemy errors, generic
      ValueError, and any unhandled exception) into the same response
      shape.
    - Centralize this translation so API routers never need local
      try/except blocks around domain exceptions.

Design Notes:
    - Every handler returns the same envelope:
          {
              "success": false,
              "error": {
                  "type": "<ExceptionClassName>",
                  "message": "<human-readable detail>",
                  "status_code": <int>,
                  "error_code": "<optional machine-readable code>",
                  "details": {"optional": "structured context"}
              }
          }
      The `error_code` and `details` keys are included only when the
      raising exception supplies them (currently: `AppException` and
      its subclasses), so the envelope shape for all pre-existing
      handlers below is completely unchanged.
      This uniformity lets API consumers write one error-parsing code
      path regardless of failure category.
    - Stack traces are NEVER included in any response body. Full
      tracebacks are only ever written to the server-side logger (via
      `logger.exception(...)` / `exc_info=True`), for unhandled
      exceptions, database errors, validation errors, and any 5xx
      `AppException`, so operators retain full diagnosability without
      leaking internals to clients.
    - Domain exception handlers import their exception classes directly
      from `app.services.lead_service`, the single source of truth for
      the Lead domain's exception hierarchy. This predates the
      `AppException` hierarchy below and is preserved unchanged for
      backward compatibility; new services should prefer raising
      `AppException` subclasses instead of repeating this pattern.
    - `register_exception_handlers(app)` is the single entry point
      `app/main.py` should call at startup to wire every handler below
      onto the FastAPI application instance.
    - Newer service modules (Document, Notification, Scheduling, etc.)
      raise a mix of "*Error" and "*Exception" suffixed names for
      concepts that already exist in the `AppException` hierarchy
      below (e.g. `ValidationError`, `NotFoundError`,
      `BusinessRuleError`/`BusinessRuleViolationError`/
      `BusinessRuleViolationException`). Rather than duplicating the
      status-code/error-code/handler logic those concepts already have,
      these are defined as direct aliases (or thin subclasses, for the
      genuinely domain-specific ones like `DocumentNotFoundError`) of
      the existing classes further down this file. They are all still
      instances of `AppException` and are therefore handled by
      `app_exception_handler` with no additional registration required.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.services.lead_service import (
    DuplicateEmailError,
    DuplicatePhoneError,
    InactiveLeadError,
    InvalidAgentAssignmentError,
    InvalidStatusTransitionError,
    LeadNotFoundError,
    TerminalLeadStatusError,
)

logger = logging.getLogger("app.exceptions")


# --------------------------------------------------------------------------
# Response Envelope Helper
# --------------------------------------------------------------------------
def _error_response(
    *,
    exc_type: str,
    message: str,
    status_code: int,
    error_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """
    Build the standardized JSON error envelope used by every exception
    handler in this module.

    Args:
        exc_type: The name of the exception class that triggered this
                   response (e.g., "LeadNotFoundError").
        message: A human-readable, client-safe description of the
                  error. Must never contain a stack trace or internal
                  implementation detail.
        status_code: The HTTP status code to return.
        error_code: Optional machine-readable error code (e.g.,
                     "DUPLICATE_RESOURCE"). Omitted from the response
                     body entirely when not provided, so callers that
                     do not pass it (all pre-existing handlers) get the
                     exact same envelope shape as before.
        details: Optional structured context (e.g., per-field
                  validation errors). Omitted from the response body
                  entirely when not provided.

    Returns:
        A `JSONResponse` with the standardized error envelope and the
        given status code.
    """
    error_payload: dict[str, Any] = {
        "type": exc_type,
        "message": message,
        "status_code": status_code,
    }
    if error_code:
        error_payload["error_code"] = error_code
    if details:
        error_payload["details"] = details

    body: dict[str, Any] = {"success": False, "error": error_payload}
    return JSONResponse(status_code=status_code, content=body)


# --------------------------------------------------------------------------
# Reusable Application Exception Hierarchy
#
# This is a centralized base hierarchy new services (Property, Customer,
# etc.) can raise directly, instead of each defining bespoke exception
# classes and per-exception handlers the way the pre-existing Lead
# domain section below does. It does not replace or alter the Lead
# domain handlers; both coexist for backward compatibility.
# --------------------------------------------------------------------------
class AppException(Exception):
    """
    Base class for all reusable, centrally-handled application
    exceptions.

    Subclasses set `default_status_code` and `default_error_code` as
    class attributes; both may still be overridden per-instance via the
    constructor when a more specific value is needed at the raise site.

    Attributes:
        message: Human-readable, client-safe description of the error.
        error_code: Short, machine-readable code identifying the error
                     category (e.g., "NOT_FOUND").
        status_code: The HTTP status code this exception maps to.
        details: Optional structured context (e.g., per-field
                  validation errors) surfaced to the client.
    """

    default_status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_error_code: str = "APP_ERROR"

    def __init__(
        self,
        message: str,
        *legacy_args: Any,
        error_code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        # Older workflow code raises NotFoundException(resource, identifier).
        # Preserve that public calling convention while keeping the modern
        # single-message form used by the rest of the application.
        if legacy_args:
            message = f"{message} '{legacy_args[0]}' not found."
        super().__init__(message)
        self.message = message
        self.error_code = error_code or self.default_error_code
        self.status_code = status_code or self.default_status_code
        self.details = details


class ValidationException(AppException):
    """Raised when input fails a domain-level validation rule (400)."""

    default_status_code = status.HTTP_400_BAD_REQUEST
    default_error_code = "VALIDATION_ERROR"


class BadRequestException(AppException):
    """Raised when a request is malformed or semantically invalid (400)."""

    default_status_code = status.HTTP_400_BAD_REQUEST
    default_error_code = "BAD_REQUEST"


class AuthenticationException(AppException):
    """Raised when a request cannot be authenticated (401)."""

    default_status_code = status.HTTP_401_UNAUTHORIZED
    default_error_code = "AUTHENTICATION_ERROR"


class AuthorizationException(AppException):
    """Raised when an authenticated user lacks permission (403)."""

    default_status_code = status.HTTP_403_FORBIDDEN
    default_error_code = "AUTHORIZATION_ERROR"


class NotFoundException(AppException):
    """Raised when a requested resource does not exist (404)."""

    default_status_code = status.HTTP_404_NOT_FOUND
    default_error_code = "NOT_FOUND"


class ConflictException(AppException):
    """Raised when a request conflicts with current resource state (409)."""

    default_status_code = status.HTTP_409_CONFLICT
    default_error_code = "CONFLICT"


class DuplicateResourceException(ConflictException):
    """
    Raised when a create/update would duplicate an existing unique
    resource (409). A specialization of `ConflictException`.
    """

    default_error_code = "DUPLICATE_RESOURCE"


class BusinessRuleException(AppException):
    """Raised when a request violates a domain business rule (422)."""

    default_status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_error_code = "BUSINESS_RULE_VIOLATION"


class DatabaseException(AppException):
    """Raised to wrap an unexpected persistence-layer failure (500)."""

    default_status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_error_code = "DATABASE_ERROR"


class ExternalServiceException(AppException):
    """Raised when a call to an upstream/external service fails (502)."""

    default_status_code = status.HTTP_502_BAD_GATEWAY
    default_error_code = "EXTERNAL_SERVICE_ERROR"


class RateLimitException(AppException):
    """Raised when a caller exceeds an allotted rate limit (429)."""

    default_status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_error_code = "RATE_LIMIT_EXCEEDED"


class FileUploadException(AppException):
    """Raised when an uploaded file fails validation or processing (400)."""

    default_status_code = status.HTTP_400_BAD_REQUEST
    default_error_code = "FILE_UPLOAD_ERROR"


# --------------------------------------------------------------------------
# Backward/Forward-Compatible Aliases
#
# Several service modules raise these generic concepts using an "Error"
# suffix (following the convention already established by the Lead
# domain's `LeadNotFoundError`, `DuplicatePhoneError`, etc.) instead of
# this hierarchy's "Exception" suffix. These are direct aliases -- not
# re-implementations -- so `error_code`, `status_code`, and handler
# dispatch (via `app_exception_handler`, since `isinstance` checks
# still hold) are guaranteed to stay identical to the canonical class.
# --------------------------------------------------------------------------
ValidationError = ValidationException
NotFoundError = NotFoundException
BusinessRuleError = BusinessRuleException
BusinessRuleViolationException = BusinessRuleException
BusinessRuleViolationError = BusinessRuleException
ForbiddenError = AuthorizationException


# --------------------------------------------------------------------------
# Domain-Specific Subclasses
#
# These represent genuinely distinct failure concepts (not just a naming
# variant of an existing class), so each is a real subclass -- not an
# alias -- of the closest-matching base above. All three still flow
# through `app_exception_handler`, so no additional handler
# registration is required.
# --------------------------------------------------------------------------
class DocumentNotFoundError(NotFoundException):
    """Raised when a requested document resource does not exist (404)."""

    default_error_code = "DOCUMENT_NOT_FOUND"


class NotificationDeliveryError(ExternalServiceException):
    """
    Raised when sending/delivering a notification (email, SMS, push,
    etc.) via an upstream provider fails (502).
    """

    default_error_code = "NOTIFICATION_DELIVERY_FAILED"


class InvalidDateRangeError(ValidationException):
    """
    Raised when a supplied date range is invalid (e.g. start date after
    end date, range exceeds an allowed span) (400).
    """

    default_error_code = "INVALID_DATE_RANGE"


class FutureDateError(ValidationException):
    """
    Raised when a supplied date that must be in the past/present (e.g.
    a report's `date_to` bound) is set in the future (400).
    """

    default_error_code = "FUTURE_DATE_NOT_ALLOWED"


class DuplicateDocumentError(DuplicateResourceException):
    """
    Raised when creating a document would duplicate an existing,
    non-deleted document (409).
    """

    default_error_code = "DUPLICATE_DOCUMENT"


class InvalidDocumentStateError(BusinessRuleException):
    """
    Raised when a document operation is not valid given the document's
    current state (422).
    """

    default_error_code = "INVALID_DOCUMENT_STATE"


class DocumentAlreadyDeletedError(ConflictException):
    """Raised when an operation targets a document that is already
    soft-deleted (409)."""

    default_error_code = "DOCUMENT_ALREADY_DELETED"


class DocumentNotDeletedError(ConflictException):
    """
    Raised when an operation (e.g. restore) requires a document to
    currently be soft-deleted, but it is not (409).
    """

    default_error_code = "DOCUMENT_NOT_DELETED"


class NotificationConflictError(ConflictException):
    """Raised when a notification operation conflicts with its current state (409)."""

    default_error_code = "NOTIFICATION_CONFLICT"


class NotificationValidationError(ValidationException):
    """Raised when a notification payload fails domain validation (400)."""

    default_error_code = "NOTIFICATION_VALIDATION_ERROR"


class TemplateNotFoundError(NotFoundException):
    """Raised when a requested notification template does not exist (404)."""

    default_error_code = "TEMPLATE_NOT_FOUND"


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """
    Handle any `AppException` subclass not caught by a more specific
    handler, translating its `status_code`, `error_code`, `message`,
    and optional `details` into the standard error envelope.

    Server-side errors (`status_code >= 500`) are logged with a full
    traceback for operator diagnosability. Client errors (4xx) are
    logged at debug level only, since they represent expected,
    validated failure paths rather than operational incidents.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `AppException` (or subclass) instance.

    Returns:
        A standardized JSON error response using `exc.status_code`.
    """
    if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        logger.error(
            "Application error [%s] on %s %s: %s",
            exc.error_code,
            request.method,
            request.url.path,
            exc.message,
            exc_info=True,
        )
    else:
        logger.debug(
            "Application error [%s] on %s %s: %s",
            exc.error_code,
            request.method,
            request.url.path,
            exc.message,
        )
    return _error_response(
        exc_type=type(exc).__name__,
        message=exc.message,
        status_code=exc.status_code,
        error_code=exc.error_code,
        details=exc.details,
    )


# --------------------------------------------------------------------------
# Lead Domain Exception Handlers
# --------------------------------------------------------------------------
async def lead_not_found_handler(request: Request, exc: LeadNotFoundError) -> JSONResponse:
    """
    Handle `LeadNotFoundError`, returning 404 Not Found.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `LeadNotFoundError` instance.

    Returns:
        A standardized 404 JSON error response.
    """
    return _error_response(
        exc_type=type(exc).__name__,
        message=str(exc),
        status_code=status.HTTP_404_NOT_FOUND,
    )


async def duplicate_phone_handler(request: Request, exc: DuplicatePhoneError) -> JSONResponse:
    """
    Handle `DuplicatePhoneError`, returning 409 Conflict.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `DuplicatePhoneError` instance.

    Returns:
        A standardized 409 JSON error response.
    """
    return _error_response(
        exc_type=type(exc).__name__,
        message=str(exc),
        status_code=status.HTTP_409_CONFLICT,
    )


async def duplicate_email_handler(request: Request, exc: DuplicateEmailError) -> JSONResponse:
    """
    Handle `DuplicateEmailError`, returning 409 Conflict.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `DuplicateEmailError` instance.

    Returns:
        A standardized 409 JSON error response.
    """
    return _error_response(
        exc_type=type(exc).__name__,
        message=str(exc),
        status_code=status.HTTP_409_CONFLICT,
    )


async def inactive_lead_handler(request: Request, exc: InactiveLeadError) -> JSONResponse:
    """
    Handle `InactiveLeadError`, returning 400 Bad Request.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `InactiveLeadError` instance.

    Returns:
        A standardized 400 JSON error response.
    """
    return _error_response(
        exc_type=type(exc).__name__,
        message=str(exc),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


async def invalid_status_transition_handler(
    request: Request, exc: InvalidStatusTransitionError
) -> JSONResponse:
    """
    Handle `InvalidStatusTransitionError`, returning 400 Bad Request.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `InvalidStatusTransitionError` instance.

    Returns:
        A standardized 400 JSON error response.
    """
    return _error_response(
        exc_type=type(exc).__name__,
        message=str(exc),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


async def invalid_agent_assignment_handler(
    request: Request, exc: InvalidAgentAssignmentError
) -> JSONResponse:
    """
    Handle `InvalidAgentAssignmentError`, returning 400 Bad Request.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `InvalidAgentAssignmentError` instance.

    Returns:
        A standardized 400 JSON error response.
    """
    return _error_response(
        exc_type=type(exc).__name__,
        message=str(exc),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


async def terminal_lead_status_handler(
    request: Request, exc: TerminalLeadStatusError
) -> JSONResponse:
    """
    Handle `TerminalLeadStatusError`, returning 400 Bad Request.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `TerminalLeadStatusError` instance.

    Returns:
        A standardized 400 JSON error response.
    """
    return _error_response(
        exc_type=type(exc).__name__,
        message=str(exc),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


# --------------------------------------------------------------------------
# Framework / Infrastructure Exception Handlers
# --------------------------------------------------------------------------

# Field-name fragments (case-insensitive) whose validation-error `input`
# value must never be written to the server log. Pydantic/FastAPI's
# `RequestValidationError.errors()` includes the raw, as-submitted value
# for every failed field under an `"input"` key -- for a field like
# `password` that value is the caller's plaintext password (e.g. a
# `string_too_short` failure on registration or change-password still
# echoes the plaintext candidate password verbatim). That must never
# reach the log stream, even on a validation failure.
_SENSITIVE_ERROR_FIELD_MARKERS = ("password", "token", "secret", "authorization")


def _redact_sensitive_error_inputs(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Return a copy of Pydantic/FastAPI validation error dicts with the
    `"input"` value redacted for any field whose location path suggests
    it holds sensitive/credential material (password, token, secret,
    etc.), so `logger.warning(...)` calls can safely log the result.

    Args:
        errors: The list of error dicts as returned by
                `RequestValidationError.errors()` / `ValidationError.errors()`.

    Returns:
        A new list of shallow-copied error dicts; entries whose `loc`
        path matches a sensitive-field marker have their `"input"` key
        replaced with the literal string `"[REDACTED]"`. Non-sensitive
        entries are returned unchanged (still shallow-copied) so the
        original `errors` list/dicts are never mutated.
    """
    redacted: list[dict[str, Any]] = []
    for error in errors:
        loc = error.get("loc", ())
        loc_text = " ".join(str(part) for part in loc).lower()
        error_copy = dict(error)
        if "input" in error_copy and any(
            marker in loc_text for marker in _SENSITIVE_ERROR_FIELD_MARKERS
        ):
            error_copy["input"] = "[REDACTED]"
        redacted.append(error_copy)
    return redacted


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Handle `RequestValidationError` (Pydantic/FastAPI request payload
    validation failures), returning 422 Unprocessable Entity.

    Logs the validation error details server-side for diagnosability,
    while returning only a client-safe summary message in the response
    body.

    Args:
        request: The incoming request that failed validation.
        exc: The raised `RequestValidationError` instance.

    Returns:
        A standardized 422 JSON error response.
    """
    logger.warning(
        "Request validation failed for %s %s: %s",
        request.method,
        request.url.path,
        _redact_sensitive_error_inputs(exc.errors()),
    )
    return _error_response(
        exc_type=type(exc).__name__,
        message="Request validation failed. Please check your input and try again.",
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


async def pydantic_validation_error_handler(
    request: Request, exc: PydanticValidationError
) -> JSONResponse:
    """
    Handle a raw `pydantic.ValidationError` raised while FastAPI is
    resolving a Pydantic-model sub-dependency (e.g. `BookingFilter`
    injected via `filters: BookingFilter = Depends()`), returning 422
    Unprocessable Entity.

    This is distinct from `RequestValidationError`: FastAPI only wraps
    field-level validation failures into `RequestValidationError` for
    parameters declared directly on a path operation function
    (`Query(...)`, `Path(...)`, etc.). When a Pydantic `BaseModel` is
    used as a `Depends()` sub-dependency and one of its own
    `@field_validator`s raises `ValueError`, Pydantic re-raises it as a
    `pydantic.ValidationError` during `__init__`, and FastAPI does NOT
    rewrap that into `RequestValidationError` -- it propagates as-is.

    Because `pydantic.ValidationError` subclasses `ValueError`, without
    this handler it would otherwise be matched (via Starlette's MRO
    walk in its exception-handler lookup) by the broader
    `value_error_handler` registered for `ValueError`, and incorrectly
    reported as 400 Bad Request instead of 422 Unprocessable Entity.
    Registering a handler for the more specific `pydantic.ValidationError`
    type takes precedence for exceptions of that exact type, without
    altering `value_error_handler`'s behavior for genuine, non-Pydantic
    `ValueError`s raised elsewhere (e.g. in the service layer).

    Args:
        request: The incoming request whose dependency failed validation.
        exc: The raised `pydantic.ValidationError` instance.

    Returns:
        A standardized 422 JSON error response.
    """
    logger.warning(
        "Dependency validation failed for %s %s: %s",
        request.method,
        request.url.path,
        _redact_sensitive_error_inputs(exc.errors()),
    )
    return _error_response(
        exc_type=type(exc).__name__,
        message="Request validation failed. Please check your input and try again.",
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """
    Handle `HTTPException`, preserving the original status code and
    detail message supplied at the raise site.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `HTTPException` instance.

    Returns:
        A standardized JSON error response using `exc.status_code` and
        `exc.detail`.
    """
    return _error_response(
        exc_type=type(exc).__name__,
        message=str(exc.detail),
        status_code=exc.status_code,
    )


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """
    Handle `IntegrityError` (e.g., unique constraint violations, foreign
    key violations), returning 409 Conflict.

    The full database error is logged server-side; only a generic,
    client-safe message is returned in the response body to avoid
    leaking schema or query details.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `IntegrityError` instance.

    Returns:
        A standardized 409 JSON error response.
    """
    logger.error(
        "Database integrity error on %s %s: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=True,
    )
    return _error_response(
        exc_type=type(exc).__name__,
        message="A data integrity conflict occurred. The resource may already exist.",
        status_code=status.HTTP_409_CONFLICT,
    )


async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """
    Handle any `SQLAlchemyError` not more specifically handled by
    `integrity_error_handler`, returning 500 Internal Server Error.

    The full database error and traceback are logged server-side; only
    a generic message is returned in the response body.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `SQLAlchemyError` instance.

    Returns:
        A standardized 500 JSON error response.
    """
    logger.error(
        "Unhandled database error on %s %s: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=True,
    )
    return _error_response(
        exc_type=type(exc).__name__,
        message="A database error occurred. Please try again later.",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """
    Handle generic `ValueError` (e.g., invalid enum values or malformed
    input surfaced outside of Pydantic validation), returning 400 Bad
    Request.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised `ValueError` instance.

    Returns:
        A standardized 400 JSON error response.
    """
    return _error_response(
        exc_type=type(exc).__name__,
        message=str(exc),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all handler for any exception not matched by a more specific
    handler above, returning 500 Internal Server Error.

    The full exception and traceback are always logged server-side via
    `logger.exception(...)`. The response body never contains the
    exception message, class internals, or stack trace, to avoid
    leaking implementation details to API clients.

    Args:
        request: The incoming request that triggered the exception.
        exc: The raised, unhandled exception instance.

    Returns:
        A standardized 500 JSON error response with a generic message.
    """
    logger.exception(
        "Unhandled exception on %s %s: %s",
        request.method,
        request.url.path,
        exc,
    )
    return _error_response(
        exc_type="InternalServerError",
        message="Internal server error. Please try again later.",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


# --------------------------------------------------------------------------
# Registration Entry Point
# --------------------------------------------------------------------------
def register_exception_handlers(app: FastAPI) -> None:
    """
    Register every exception handler defined in this module onto the
    given FastAPI application instance.

    This is the single entry point `app/main.py` should call once at
    startup (e.g., `register_exception_handlers(app)` immediately after
    constructing `app = FastAPI(...)`), eliminating the need for any
    try/except block around domain exceptions inside API routers.

    Handler registration order matters for exception-hierarchy
    specificity: `IntegrityError` is registered before the broader
    `SQLAlchemyError` so that integrity violations are matched by the
    more specific 409 handler rather than falling through to the
    generic 500 handler. Likewise, `pydantic.ValidationError` is
    registered as its own, more specific entry ahead of `ValueError`:
    since `pydantic.ValidationError` is a subclass of `ValueError`,
    without its own registration it would otherwise be matched by the
    broader `ValueError` handler. FastAPI resolves handlers by the most
    specific matching exception type regardless of registration order,
    but the explicit ordering here documents that intent clearly.

    The reusable `AppException` handler is registered independently of
    the Lead domain and framework/infrastructure handlers, since
    `AppException` and its subclasses form their own hierarchy separate
    from `LeadService`'s pre-existing exception classes; there is no
    overlap or precedence conflict between the two. All of the
    alias/subclass names defined above (`ValidationError`,
    `NotFoundError`, `BusinessRuleError`, `BusinessRuleViolationError`,
    `BusinessRuleViolationException`, `DocumentNotFoundError`,
    `NotificationDeliveryError`, `InvalidDateRangeError`) are instances
    of `AppException`, so they are already covered by this single
    registration and require no handlers of their own.

    Args:
        app: The FastAPI application instance to register handlers on.

    Returns:
        None.
    """
    # Lead domain exceptions
    app.add_exception_handler(LeadNotFoundError, lead_not_found_handler)
    app.add_exception_handler(DuplicatePhoneError, duplicate_phone_handler)
    app.add_exception_handler(DuplicateEmailError, duplicate_email_handler)
    app.add_exception_handler(InactiveLeadError, inactive_lead_handler)
    app.add_exception_handler(InvalidStatusTransitionError, invalid_status_transition_handler)
    app.add_exception_handler(InvalidAgentAssignmentError, invalid_agent_assignment_handler)
    app.add_exception_handler(TerminalLeadStatusError, terminal_lead_status_handler)

    # Reusable application exception hierarchy (for new/future services)
    app.add_exception_handler(AppException, app_exception_handler)

    # Framework / infrastructure exceptions
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(PydanticValidationError, pydantic_validation_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)
    app.add_exception_handler(ValueError, value_error_handler)

    # Catch-all
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = [
    "register_exception_handlers",
    "AppException",
    "ValidationException",
    "BadRequestException",
    "AuthenticationException",
    "AuthorizationException",
    "NotFoundException",
    "ConflictException",
    "DuplicateResourceException",
    "BusinessRuleException",
    "DatabaseException",
    "ExternalServiceException",
    "RateLimitException",
    "FileUploadException",
    # Backward/forward-compatible aliases (see note above the class
    # definitions for rationale).
    "ValidationError",
    "NotFoundError",
    "BusinessRuleError",
    "BusinessRuleViolationException",
    "BusinessRuleViolationError",
    "ForbiddenError",
    # Domain-specific subclasses
    "DocumentNotFoundError",
    "NotificationDeliveryError",
    "InvalidDateRangeError",
    "FutureDateError",
    "DuplicateDocumentError",
    "InvalidDocumentStateError",
    "DocumentAlreadyDeletedError",
    "DocumentNotDeletedError",
    "NotificationConflictError",
    "NotificationValidationError",
    "TemplateNotFoundError",
]