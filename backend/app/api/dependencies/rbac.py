"""Role-based access-control dependency factory.

The RBAC layer depends directly on the canonical authentication dependency,
not on ``app.api.deps``.  ``app.api.deps`` re-exports this factory for legacy
imports.  Keeping this direction one-way prevents the circular import that
previously occurred while FastAPI was importing the routers.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from fastapi import Depends, HTTPException, status

from app.api.dependencies.auth_dependency import get_current_user
from app.models.user import User


_ROLE_ALIASES = {
    "admin": "ADMIN",
    "administrator": "ADMIN",
    "manager": "SALES_MANAGER",
    "sales_manager": "SALES_MANAGER",
    "sales-manager": "SALES_MANAGER",
    "agent": "SALES_AGENT",
    "sales_agent": "SALES_AGENT",
    "sales-agent": "SALES_AGENT",
    "sales_rep": "SALES_AGENT",
    "sales-rep": "SALES_AGENT",
    "viewer": "READ_ONLY",
    "read_only": "READ_ONLY",
    "read-only": "READ_ONLY",
    "readonly": "READ_ONLY",
    "auditor": "AUDITOR",
}


def _normalize_role(value: Any) -> str:
    raw = getattr(value, "value", value)
    text = str(raw).strip()
    return _ROLE_ALIASES.get(text.lower(), text.upper())


def require_roles(
    *allowed_roles: Any,
) -> Callable[..., Coroutine[Any, Any, User]]:
    """Create a FastAPI dependency requiring one of ``allowed_roles``.

    ``get_current_user`` is the exact canonical dependency object imported by
    the routers.  Therefore an override such as
    ``app.dependency_overrides[get_current_user] = ...`` also overrides the
    authentication step nested inside this RBAC dependency.
    """
    normalized_allowed = {
        _normalize_role(role)
        for role in allowed_roles
        if role is not None
    }

    async def _role_checker(
        current_user: User = Depends(get_current_user),
    ) -> User:
        raw_roles = getattr(current_user, "roles", None)

        if isinstance(raw_roles, (list, tuple, set, frozenset)) and raw_roles:
            candidate_roles = raw_roles
        else:
            candidate_roles = [getattr(current_user, "role", None)]

        user_roles = {
            _normalize_role(role)
            for role in candidate_roles
            if role is not None
        }

        if not user_roles.intersection(normalized_allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )

        return current_user

    return _role_checker


__all__ = ["require_roles"]
