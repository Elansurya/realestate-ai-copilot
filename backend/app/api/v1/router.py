"""
backend/app/api/v1/router.py

Partial v1 router aggregator (auth, users, lead, property, customer).

NOTE: The application's actual mounted router is built in
`app/api/v1/__init__.py`, which aggregates every v1 module (including
the ones below plus booking, payment, dashboard, report, notification,
audit_log, settings, document, workflow, activity, task, search,
integration, monitoring, and webhook) and is what `app/main.py` mounts
under the `/api/v1` prefix. This file previously imported from a
nonexistent `app.api.v1.endpoints` package (an empty directory with no
`auth`/`users`/`lead`/`property`/`customer` modules inside it) and could
never import successfully. It has been corrected to import each router
directly from its real module location under `app.api.v1`, matching the
convention used by `app/api/v1/__init__.py`.
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.lead import router as lead_router
from app.api.v1.property import router as property_router
from app.api.v1.customer import router as customer_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(lead_router)
api_router.include_router(property_router)
api_router.include_router(customer_router)

__all__ = ["api_router"]