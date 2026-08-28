"""Canonical FastAPI dependency compatibility facade.

This module deliberately contains no dependency implementations.  It exposes
one shared callable for each cross-cutting dependency so FastAPI dependency
overrides used by the test suite (and by application integrations) always
match the dependency object used inside the actual route graph.
"""

from __future__ import annotations

from app.api.dependencies.auth_dependency import get_current_user
from app.api.dependencies.rbac import require_roles
from app.db.session import get_db

# Historical name retained for callers that imported it from this module.
get_db_session = get_db

__all__ = [
    "get_current_user",
    "get_db",
    "get_db_session",
    "require_roles",
]
