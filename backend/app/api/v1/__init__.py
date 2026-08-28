"""
backend/app/api/v1/__init__.py

Aggregates all v1 API routers into a single `api_router` that
`app.main` mounts under the `/api/v1` prefix.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.lead import router as lead_router
from app.api.v1.customer import router as customer_router
from app.api.v1.property import router as property_router
from app.api.v1.booking import router as booking_router
from app.api.v1.payment import router as payment_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.report import router as report_router
from app.api.v1.ai import router as ai_router
from app.api.v1.notification import router as notification_router
from app.api.v1.audit_log import router as audit_log_router
from app.api.v1.settings import router as settings_router
from app.api.v1.document import router as document_router
from app.api.v1.workflow import router as workflow_router
from app.api.v1.activity import router as activity_router
from app.api.v1.task import router as task_router
from app.api.v1.search import router as search_router
from app.api.v1.integration import router as integration_router
from app.api.v1.monitoring import router as monitoring_router
from app.api.v1.webhook import router as webhook_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(lead_router)
api_router.include_router(customer_router)
api_router.include_router(property_router)
api_router.include_router(booking_router)
api_router.include_router(payment_router)
api_router.include_router(dashboard_router)
api_router.include_router(report_router)
api_router.include_router(ai_router)
api_router.include_router(notification_router)
api_router.include_router(audit_log_router)
api_router.include_router(settings_router)
api_router.include_router(document_router)
api_router.include_router(workflow_router)
api_router.include_router(activity_router)
api_router.include_router(task_router)
api_router.include_router(search_router)
api_router.include_router(integration_router)
api_router.include_router(monitoring_router)
api_router.include_router(webhook_router)

__all__ = ["api_router"]