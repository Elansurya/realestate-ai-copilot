"""
Application SQLAlchemy model package.

All ORM models are imported here so they are registered with the shared
Base.metadata used by the application and Alembic.

Optional integrations are imported defensively where their third-party
dependencies may not be installed in lightweight/test environments.
"""

from app.db.base import Base

# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------

from app.models.user import User, UserRole

from app.models.lead import (
    Lead,
    LeadPriority,
    LeadSource,
    LeadStatus,
)

from app.models.property import (
    FurnishingType,
    ListingType,
    Property,
    PropertyStatus,
    PropertyType,
)

from app.models.customer import (
    Customer,
    CustomerSource,
    CustomerStatus,
    CustomerType,
    Gender,
    MaritalStatus,
    PreferredBHK,
    PreferredPropertyType,
)

from app.models.booking import (
    Booking,
    BookingPaymentMode,
    BookingPaymentStatus,
    BookingStatus,
)

from app.models.payment import (
    Payment,
    PaymentMode,
    PaymentStatus,
    PaymentType,
)

# ---------------------------------------------------------------------------
# Activity / audit
# ---------------------------------------------------------------------------

from app.models.activity import (
    Activity,
    ActivityModule,
    ActivityPriority,
    ActivityStatus,
    ActivityType,
)

from app.models.audit_log import (
    AuditAction,
    AuditLog,
    AuditSeverity,
    AuditStatus,
)

# ---------------------------------------------------------------------------
# AI / conversation
# ---------------------------------------------------------------------------

from app.models.conversation import Conversation
from app.models.message import Message, MessageRole
from app.models.prompt_template import PromptCategory, PromptTemplate

from app.models.knowledge_document import (
    DocumentSourceType,
    DocumentStatus,
    KnowledgeDocument,
)

# Embedding depends on pgvector. Keep it optional so modules such as
# Global Search can still import in environments where pgvector is absent.
try:
    from app.models.embedding import Embedding
except (ImportError, ModuleNotFoundError):
    Embedding = None  # type: ignore[assignment]

from app.models.ai_usage import (
    AIFeature,
    AIUsage,
    AIUsageStatus,
)

# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

from app.models.document import (
    Document,
    DocumentCategory,
    DocumentFileType,
    DocumentStorageProvider,
)

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

from app.models.notification import (
    Notification,
    NotificationCategory,
    NotificationChannel,
    NotificationPriority,
    NotificationStatus,
    SoftDeleteMixin,
    TimestampMixin,
)

from app.models.notification_log import (
    NotificationEventType,
    NotificationLog,
)

from app.models.notification_queue import (
    NotificationQueue,
    QueueStatus,
)

from app.models.notification_template import (
    NotificationTemplate,
    TemplateLocale,
)

from app.models.email_notification import (
    EmailNotification,
    EmailProvider,
)

from app.models.sms_notification import (
    SMSDeliveryStatus,
    SMSNotification,
    SMSProvider,
)

from app.models.whatsapp_notification import (
    WhatsAppMessageType,
    WhatsAppNotification,
    WhatsAppProvider,
)

from app.models.push_notification import (
    DevicePlatform,
    PushNotification,
    PushProvider,
)

from app.models.in_app_notification import (
    InAppDisplayType,
    InAppNotification,
)

# ---------------------------------------------------------------------------
# Integrations / webhooks
# ---------------------------------------------------------------------------

from app.models.integration import (
    Integration,
    IntegrationProvider,
    IntegrationStatus,
    IntegrationType,
)

from app.models.integration import (
    AuthenticationType as IntegrationAuthenticationType,
)

from app.models.webhook import (
    DeliveryStatus,
    Webhook,
    WebhookEvent,
    WebhookLog,
    WebhookStatus,
)

from app.models.webhook import (
    AuthenticationType as WebhookAuthenticationType,
)

# ---------------------------------------------------------------------------
# Tasks / workflows
# ---------------------------------------------------------------------------

from app.models.task import (
    Task,
    TaskPriority,
    TaskStatus,
    TaskType,
)

from app.models.workflow import (
    ApprovalStatus,
    Workflow,
    WorkflowApproval,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

from app.models.search import (
    SearchHistory,
    SearchModule,
    SearchType,
)

# ---------------------------------------------------------------------------
# Settings / monitoring
# ---------------------------------------------------------------------------

from app.models.settings import (
    SettingCategory,
    SettingDataType,
    Settings,
)

from app.models.monitoring import (
    ComponentType,
    HealthStatus,
    MetricType,
    SystemHealth,
)

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

from app.models.dashboard import (
    BookingStatusEnum,
    LeadStatusEnum,
    PaymentStatusEnum,
    PropertyStatusEnum,
    TrendPeriod,
)

from app.models.dashboard import (
    ActivityType as DashboardActivityType,
)

# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

from app.models.report import (
    ExportFormat,
    ReportBookingStatus,
    ReportLeadSource,
    ReportLeadStatus,
    ReportPaymentStatus,
    ReportPeriod,
    ReportPropertyStatus,
    ReportType,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "Base",

    # User
    "User",
    "UserRole",

    # Lead
    "Lead",
    "LeadPriority",
    "LeadSource",
    "LeadStatus",

    # Property
    "Property",
    "PropertyType",
    "PropertyStatus",
    "ListingType",
    "FurnishingType",

    # Customer
    "Customer",
    "CustomerType",
    "CustomerStatus",
    "CustomerSource",
    "Gender",
    "MaritalStatus",
    "PreferredPropertyType",
    "PreferredBHK",

    # Booking
    "Booking",
    "BookingStatus",
    "BookingPaymentStatus",
    "BookingPaymentMode",

    # Payment
    "Payment",
    "PaymentStatus",
    "PaymentMode",
    "PaymentType",

    # Activity
    "Activity",
    "ActivityType",
    "ActivityModule",
    "ActivityPriority",
    "ActivityStatus",

    # Audit
    "AuditLog",
    "AuditAction",
    "AuditSeverity",
    "AuditStatus",

    # AI
    "Conversation",
    "Message",
    "MessageRole",
    "PromptTemplate",
    "PromptCategory",
    "KnowledgeDocument",
    "DocumentSourceType",
    "DocumentStatus",
    "Embedding",
    "AIUsage",
    "AIFeature",
    "AIUsageStatus",

    # Documents
    "Document",
    "DocumentFileType",
    "DocumentCategory",
    "DocumentStorageProvider",

    # Notifications
    "Notification",
    "NotificationChannel",
    "NotificationPriority",
    "NotificationStatus",
    "NotificationCategory",
    "TimestampMixin",
    "SoftDeleteMixin",
    "NotificationLog",
    "NotificationEventType",
    "NotificationQueue",
    "QueueStatus",
    "NotificationTemplate",
    "TemplateLocale",
    "EmailNotification",
    "EmailProvider",
    "SMSNotification",
    "SMSProvider",
    "SMSDeliveryStatus",
    "WhatsAppNotification",
    "WhatsAppProvider",
    "WhatsAppMessageType",
    "PushNotification",
    "DevicePlatform",
    "PushProvider",
    "InAppNotification",
    "InAppDisplayType",

    # Integration
    "Integration",
    "IntegrationType",
    "IntegrationProvider",
    "IntegrationStatus",
    "IntegrationAuthenticationType",

    # Webhook
    "Webhook",
    "WebhookLog",
    "WebhookStatus",
    "WebhookEvent",
    "DeliveryStatus",
    "WebhookAuthenticationType",

    # Task
    "Task",
    "TaskStatus",
    "TaskPriority",
    "TaskType",

    # Workflow
    "Workflow",
    "WorkflowStep",
    "WorkflowApproval",
    "WorkflowStatus",
    "WorkflowStepStatus",
    "ApprovalStatus",

    # Search
    "SearchHistory",
    "SearchModule",
    "SearchType",

    # Settings
    "Settings",
    "SettingCategory",
    "SettingDataType",

    # Monitoring
    "SystemHealth",
    "HealthStatus",
    "ComponentType",
    "MetricType",

    # Dashboard
    "DashboardActivityType",
    "TrendPeriod",
    "LeadStatusEnum",
    "BookingStatusEnum",
    "PaymentStatusEnum",
    "PropertyStatusEnum",

    # Reports
    "ReportType",
    "ReportPeriod",
    "ExportFormat",
    "ReportLeadSource",
    "ReportLeadStatus",
    "ReportBookingStatus",
    "ReportPaymentStatus",
    "ReportPropertyStatus",
]