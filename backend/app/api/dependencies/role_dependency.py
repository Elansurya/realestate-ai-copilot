"""
backend/app/api/dependencies/role_dependency.py

Reusable FastAPI dependencies implementing Role-Based Access Control
(RBAC) on top of the authenticated user resolved by
`app.api.dependencies.auth_dependency.get_current_user`.

Responsibilities:
    - Verify that the authenticated user holds one of the permitted
      roles for a given route.
    - Raise HTTPException(403) when the user is authenticated but lacks
      sufficient privileges (distinct from 401, which signals missing/
      invalid authentication).

Design Notes:
    - Authorization is strictly layered on top of authentication: every
      dependency here first depends on `get_current_user`, so a request
      must already carry a valid access token before role checks occur.
    - A single generic factory (`_require_roles`) builds all
      role-checking dependencies to guarantee consistent behavior
      (status code, error message shape) and to avoid duplicated
      branching logic across role-specific dependencies.
    - `require_admin_or_manager` is implemented via the same factory
      with multiple allowed roles, demonstrating the reusable/composable
      design requested rather than ad-hoc duplication.
"""

from __future__ import annotations

from typing import Callable, Iterable

from fastapi import Depends, HTTPException, status

from app.api.dependencies.auth_dependency import get_current_user
from app.models.user import User, UserRole


def _require_roles(*allowed_roles: UserRole) -> Callable[[User], User]:
    """
    Build a FastAPI dependency that authorizes only users whose `role`
    is within `allowed_roles`.

    Args:
        *allowed_roles: One or more `UserRole` values permitted to access
                         the protected route.

    Returns:
        A dependency-callable that, given the current authenticated
        user, either returns that user (if authorized) or raises
        HTTPException(403).

    Notes:
        - Centralizing the check here ensures every role-based
          dependency below shares identical error semantics, making
          authorization failures predictable and easy to test.
    """

    def _dependency(current_user: User = Depends(get_current_user)) -> User:
        """
        Verify that `current_user.role` is one of the allowed roles.

        Args:
            current_user: The authenticated `User`, resolved and
                          validated by `get_current_user`.

        Returns:
            The `current_user` unchanged, if authorized.

        Raises:
            HTTPException(403): If the user's role is not permitted to
                access the protected resource.
        """
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have sufficient permissions to perform this action.",
            )
        return current_user

    return _dependency


def _format_roles(roles: Iterable[UserRole]) -> str:  # pragma: no cover - debug aid
    """
    Utility for producing a human-readable list of role names, useful
    for logging/debugging dependency construction (not used in error
    responses, to avoid leaking internal role structure to clients).
    """
    return ", ".join(role.value for role in roles)


# --------------------------------------------------------------------------
# Single-Role Dependencies
# --------------------------------------------------------------------------
require_admin = _require_roles(UserRole.ADMIN)
"""
Dependency restricting access to users with the ADMIN role only.

Usage:
    @router.get("/admin-only")
    async def admin_route(user: User = Depends(require_admin)):
        ...
"""

require_sales_manager = _require_roles(UserRole.SALES_MANAGER)
"""
Dependency restricting access to users with the SALES_MANAGER role only.

Usage:
    @router.get("/manager-only")
    async def manager_route(user: User = Depends(require_sales_manager)):
        ...
"""

require_sales_agent = _require_roles(UserRole.SALES_AGENT)
"""
Dependency restricting access to users with the SALES_AGENT role only.

Usage:
    @router.get("/agent-only")
    async def agent_route(user: User = Depends(require_sales_agent)):
        ...
"""


# --------------------------------------------------------------------------
# Multi-Role Dependency
# --------------------------------------------------------------------------
require_admin_or_manager = _require_roles(UserRole.ADMIN, UserRole.SALES_MANAGER)
"""
Dependency restricting access to users with either the ADMIN or
SALES_MANAGER role — typical for management-level operations (e.g.,
team oversight, reporting) that should not be exposed to individual
sales agents.

Usage:
    @router.get("/management")
    async def management_route(user: User = Depends(require_admin_or_manager)):
        ...
"""


__all__ = [
    "require_admin",
    "require_sales_manager",
    "require_sales_agent",
    "require_admin_or_manager",
]