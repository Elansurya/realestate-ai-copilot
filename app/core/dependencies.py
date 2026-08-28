"""
Centralized FastAPI dependency module.

This module exposes reusable dependency functions for:
  * Database session acquisition
  * Service layer instantiation (repository -> service wiring)
  * JWT-authenticated user resolution
  * Role-based access control (RBAC) guards

It contains NO business logic and NO SQL queries. Its sole
responsibility is dependency injection wiring, so that the API layer
can `Depends()` on a fully constructed, ready-to-use object.
"""

from enum import Enum
from typing import Iterable

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_async_session
from app.api.dependencies.auth_dependency import get_current_user
from app.models.user import User
from app.repositories.customer_repository import CustomerRepository
from app.repositories.lead_repository import LeadRepository
from app.repositories.property_repository import PropertyRepository
from app.repositories.user_repository import UserRepository
from app.services.auth_service import AuthService
from app.services.customer_service import CustomerService
from app.services.lead_service import LeadService
from app.services.property_service import PropertyService
from app.services.user_service import UserService


# ---------------------------------------------------------------------------
# Role enum
# ---------------------------------------------------------------------------
class UserRole(str, Enum):
    """Canonical RBAC role identifiers used across the application."""

    ADMIN = "ADMIN"
    MANAGER = "SALES_MANAGER"
    SALES_AGENT = "SALES_AGENT"
    READ_ONLY = "read_only"

# ---------------------------------------------------------------------------
# Database dependency (re-exported for convenience)
# ---------------------------------------------------------------------------
async def get_db_session(
    session: AsyncSession = Depends(get_async_session),
) -> AsyncSession:
    """Resolve the request-scoped async database session.

    Thin re-export of `app.core.database.get_async_session` so that all
    dependency wiring can be imported from a single module.
    """
    return session


# Backward/alternate-name alias expected by some API routers.
get_db = get_db_session


# ---------------------------------------------------------------------------
# Service dependencies
#
# Each function follows the same wiring pattern:
#   1. Resolve the request-scoped AsyncSession.
#   2. Instantiate the corresponding Repository with that session.
#   3. Instantiate the corresponding Service with (session, repository).
#
# When a new module is added (e.g. BookingService, PaymentService,
# DocumentService, NotificationService, AgentService), add a new
# `get_<module>_service` function here following this exact pattern so
# the dependency layer stays consistent and predictable across modules.
# ---------------------------------------------------------------------------
def get_customer_service(
    session: AsyncSession = Depends(get_async_session),
) -> CustomerService:
    """Build a `CustomerService` bound to a request-scoped session.

    Wiring: `CustomerRepository(session)` -> `CustomerService(session, repository)`.
    """
    repository = CustomerRepository(session)
    return CustomerService(session, repository)


def get_lead_service(
    session: AsyncSession = Depends(get_async_session),
) -> LeadService:
    """Build a `LeadService` bound to a request-scoped session.

    Wiring: `LeadRepository(session)` -> `LeadService(session, repository)`.
    """
    repository = LeadRepository(session)
    return LeadService(session, repository)


def get_property_service(
    session: AsyncSession = Depends(get_async_session),
) -> PropertyService:
    """Build a `PropertyService` bound to a request-scoped session.

    Wiring: `PropertyRepository(session)` -> `PropertyService(session, repository)`.
    """
    repository = PropertyRepository(session)
    return PropertyService(session, repository)


def get_user_service(
    session: AsyncSession = Depends(get_async_session),
) -> UserService:
    """Build a `UserService` bound to a request-scoped session.

    Wiring: `UserRepository(session)` -> `UserService(session, repository)`.
    """
    repository = UserRepository(session)
    return UserService(session, repository)


def get_auth_service(
    session: AsyncSession = Depends(get_async_session),
) -> AuthService:
    """Build an `AuthService` bound to a request-scoped session.

    Wiring: `UserRepository(session)` -> `AuthService(session, repository)`.
    """
    repository = UserRepository(session)
    return AuthService(session, repository)


# ---------------------------------------------------------------------------
# Authenticated user dependencies
# ---------------------------------------------------------------------------
async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Resolve the authenticated user and ensure the account is active.

    Builds on the existing JWT-based `get_current_user` dependency from
    `app.api.dependencies.auth_dependency`, adding an activation check on
    top.

    Raises:
        HTTPException: 403 if the resolved user account is inactive.
    """
    if not getattr(current_user, "is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user account.",
        )
    return current_user


# ---------------------------------------------------------------------------
# Generic role-based access control guard
# ---------------------------------------------------------------------------
class RoleChecker:
    """Reusable dependency that enforces membership in an allowed role set.

    This is the single RBAC building block for this application: it is
    the sole implementation of role enforcement, and the named helper
    dependencies below (`get_current_admin`, `get_current_manager`,
    `get_current_sales_agent`, `get_current_read_only_user`) are each
    thin, pre-configured wrappers around a `RoleChecker` instance.

    Usage:
        Depends(RoleChecker([UserRole.ADMIN, UserRole.MANAGER]))
    """

    def __init__(self, allowed_roles: Iterable[UserRole]) -> None:
        self.allowed_roles = set(allowed_roles)

    async def __call__(
        self,
        current_user: User = Depends(get_current_active_user),
    ) -> User:
        raw_role = getattr(current_user, "role", None)
        role_value = getattr(raw_role, "value", raw_role)
        role_aliases = {
            "admin": "ADMIN",
            "administrator": "ADMIN",
            "manager": "SALES_MANAGER",
            "sales_manager": "SALES_MANAGER",
            "agent": "SALES_AGENT",
            "sales_agent": "SALES_AGENT",
            "sales_rep": "SALES_AGENT",
            "viewer": "read_only",
            "read-only": "read_only",
        }
        normalized_user_role = role_aliases.get(
            str(role_value).strip().lower(), str(role_value).strip().upper()
        )
        normalized_allowed = {
            role_aliases.get(
                str(getattr(role, "value", role)).strip().lower(),
                str(getattr(role, "value", role)).strip().upper(),
            )
            for role in self.allowed_roles
        }
        if normalized_user_role not in normalized_allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )
        return current_user


# ---------------------------------------------------------------------------
# Named role helpers
# ---------------------------------------------------------------------------
async def get_current_admin(
    current_user: User = Depends(RoleChecker([UserRole.ADMIN])),
) -> User:
    """Require the authenticated user to hold the Admin role."""
    return current_user


async def get_current_manager(
    current_user: User = Depends(
        RoleChecker([UserRole.ADMIN, UserRole.MANAGER])
    ),
) -> User:
    """Require the authenticated user to hold the Manager role (or Admin)."""
    return current_user


async def get_current_sales_agent(
    current_user: User = Depends(
        RoleChecker([UserRole.ADMIN, UserRole.MANAGER, UserRole.SALES_AGENT])
    ),
) -> User:
    """Require the authenticated user to hold the Sales Agent role or above."""
    return current_user


async def get_current_read_only_user(
    current_user: User = Depends(
        RoleChecker(
            [
                UserRole.ADMIN,
                UserRole.MANAGER,
                UserRole.SALES_AGENT,
                UserRole.READ_ONLY,
            ]
        )
    ),
) -> User:
    """Require the authenticated user to hold at least Read Only access."""
    return current_user