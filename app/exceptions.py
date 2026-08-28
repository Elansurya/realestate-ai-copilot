"""
backend/app/exceptions.py

Domain-level exception hierarchy for the Customer service layer.

`app.services.customer_service.CustomerService` raises these directly;
`app.api.v1.customers` catches them and translates each into the
matching HTTP status code. This module contains no FastAPI/HTTP
concerns of its own -- translation to `HTTPException` happens entirely
in the API layer.

Note: this is intentionally separate from `app.core.exceptions`, which
defines a broader `AppException` hierarchy (`NotFoundException`,
`ConflictException`, etc.) originally built around the Lead domain.
The two hierarchies do not share a common base class today; if you
later want `CustomerServiceError` and friends to also be caught by
`app.core.exceptions.app_exception_handler`, make `CustomerServiceError`
extend `app.core.exceptions.AppException` instead of `Exception`.
"""

from __future__ import annotations


class CustomerServiceError(Exception):
    """Base class for all Customer-domain service-layer exceptions.

    Also raised directly (rather than via a subclass) to represent an
    unexpected persistence-layer failure that has already been logged
    and rolled back by the service, and should surface to the client
    as a generic 500 with no internal detail attached.
    """


class CustomerNotFoundError(CustomerServiceError):
    """Raised when a referenced Customer record does not exist."""


class DuplicateCustomerError(CustomerServiceError):
    """Raised when a create/update would violate the Customer email
    uniqueness constraint."""


class InvalidCustomerStateError(CustomerServiceError):
    """Raised when an operation is not valid given the Customer's
    current state (e.g. soft-deleting an already-inactive customer,
    scheduling a follow-up date in the past)."""


class LeadNotFoundError(CustomerServiceError):
    """Raised when a Customer payload references a Lead id that does
    not exist."""


class UserNotFoundError(CustomerServiceError):
    """Raised when a Customer payload references a User id (e.g. an
    assignee) that does not exist."""


__all__ = [
    "CustomerServiceError",
    "CustomerNotFoundError",
    "DuplicateCustomerError",
    "InvalidCustomerStateError",
    "LeadNotFoundError",
    "UserNotFoundError",
]