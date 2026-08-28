"""Compatibility namespace for notification API tests and legacy callers."""
from app.services.notification_service import (
    NotificationService,
    NotificationTemplateService,
    NotificationQueueService,
    NotificationDispatchService,
)
notification_service = NotificationService
queue_service = NotificationQueueService
template_service = NotificationTemplateService
dispatch_service = NotificationDispatchService
__all__ = ["notification_service", "queue_service", "template_service", "dispatch_service"]
