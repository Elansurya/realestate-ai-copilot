"""
Notification Module - Phase 5
Service Layer Test Suite

Covers:
    - Email Service
    - SMS Service
    - WhatsApp Service
    - Push Service
    - In-App Service
    - Notification Service (orchestrator)
    - Queue Service
    - Scheduler Service
    - Template Service
    - Retry Logic
    - Bulk Notifications
    - Delivery Tracking
    - Validation
    - Business Rules

All external channel providers (SMTP, Twilio, Meta Cloud API, FCM) are
mocked via their respective `*ProviderInterface` contracts. These tests
validate orchestration, business rules, and error handling in the
service layer in isolation from the transport layer.

NOTE ON REWRITE: This file was rewritten from a version targeting an
older notification-service architecture (single unified `send()` method
per channel service, a `tenant_id`-scoped schema layer, and a
multi-tenant debounce/mute business-rule layer). The current production
code has no `tenant_id` concept and no per-recipient debounce/mute
suppression logic anywhere in `NotificationService`, the channel
services, or the repositories. Two tests that depended entirely on that
removed behavior are intentionally skipped with a documented reason
(see `TestBusinessRules`) rather than invented against non-existent
APIs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.core.email_provider import EmailSendResult
from app.core.notification_settings import QueueConfig, RetryConfig
from app.core.push_provider import PushSendResult
from app.core.sms_provider import SMSSendResult
from app.core.whatsapp_provider import WhatsAppSendResult
from app.models.email_notification import EmailProvider
from app.models.in_app_notification import InAppDisplayType
from app.models.notification import (
    NotificationCategory,
    NotificationChannel,
    NotificationPriority,
    NotificationStatus,
)
from app.models.notification_template import TemplateLocale
from app.models.push_notification import DevicePlatform, PushProvider
from app.models.sms_notification import SMSDeliveryStatus, SMSProvider
from app.models.whatsapp_notification import WhatsAppMessageType, WhatsAppProvider
from app.schemas.notification import BulkNotificationCreate, NotificationCreate
from app.schemas.template import TemplateCreate
from app.services.email_service import EmailService
from app.services.in_app_service import InAppService
from app.services.notification_service import (
    InvalidNotificationStateError,
    NotificationNotFoundError,
    RateLimitExceededError,
    RetryLimitExceededError,
    SchedulingError,
    NotificationService,
    TemplateNotFoundError as ServiceTemplateNotFoundError,
    TemplateRenderError,
)
from app.services.push_service import PushService
from app.services.queue_service import BatchProcessResult, QueueService
from app.services.scheduler_service import SchedulerService
from app.services.sms_service import SMSService
from app.services.template_service import TemplateService
from app.services.whatsapp_service import WhatsAppService

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def recipient_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def sender_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def notification_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def mock_session():
    """A stand-in async SQLAlchemy session.

    `.add` is a plain (synchronous) call on a real `AsyncSession`, so it
    is left as the default `MagicMock` attribute rather than wrapped in
    `AsyncMock`; `.flush` is awaited by the production code, so it must
    resolve as a coroutine.
    """
    session = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture
def mock_notification_repo():
    return AsyncMock()


@pytest.fixture
def mock_queue_repo():
    repo = AsyncMock()

    repo.enqueue = AsyncMock()
    repo.bulk_enqueue = AsyncMock()
    repo.fetch_next_batch = AsyncMock()
    repo.mark_completed = AsyncMock()
    repo.mark_failed = AsyncMock()
    repo.schedule_retry = AsyncMock()
    repo.get_by_notification_id = AsyncMock()
    repo.cancel = AsyncMock()
    repo.reschedule = AsyncMock()
    repo.release_stale_locks = AsyncMock()

    return repo


@pytest.fixture
def mock_log_repo():
    return AsyncMock()


@pytest.fixture
def mock_template_repo():
    return AsyncMock()


@pytest.fixture
def mock_email_provider():
    provider = AsyncMock()
    provider.send.return_value = EmailSendResult(
        success=True, provider_message_id="smtp-msg-001"
    )
    return provider


@pytest.fixture
def mock_sms_provider():
    provider = AsyncMock()
    provider.send.return_value = SMSSendResult(success=True, provider_message_id="SM123456")
    return provider


@pytest.fixture
def mock_whatsapp_provider():
    provider = AsyncMock()
    provider.send.return_value = WhatsAppSendResult(
        success=True, provider_message_id="wamid.HBg1"
    )
    return provider


@pytest.fixture
def mock_push_provider():
    provider = AsyncMock()
    provider.send.return_value = PushSendResult(success=True, provider_message_id="fcm-msg-001")
    return provider


@pytest.fixture
def email_service(mock_session, mock_notification_repo, mock_log_repo, mock_email_provider) -> EmailService:
    return EmailService(
        session=mock_session,
        notification_repo=mock_notification_repo,
        log_repo=mock_log_repo,
        provider=mock_email_provider,
        provider_name=EmailProvider.SMTP,
        default_sender_email="no-reply@realestateco.com",
    )


@pytest.fixture
def sms_service(mock_session, mock_notification_repo, mock_log_repo, mock_sms_provider) -> SMSService:
    return SMSService(
        session=mock_session,
        notification_repo=mock_notification_repo,
        log_repo=mock_log_repo,
        provider=mock_sms_provider,
        provider_name=SMSProvider.TWILIO,
        default_sender_number="+15550000000",
    )


@pytest.fixture
def whatsapp_service(
    mock_session, mock_notification_repo, mock_log_repo, mock_whatsapp_provider
) -> WhatsAppService:
    return WhatsAppService(
        session=mock_session,
        notification_repo=mock_notification_repo,
        log_repo=mock_log_repo,
        provider=mock_whatsapp_provider,
        provider_name=WhatsAppProvider.META_CLOUD_API,
        default_business_number="+15551112222",
    )


@pytest.fixture
def push_service(mock_session, mock_notification_repo, mock_log_repo, mock_push_provider) -> PushService:
    return PushService(
        session=mock_session,
        notification_repo=mock_notification_repo,
        log_repo=mock_log_repo,
        provider=mock_push_provider,
        provider_name=PushProvider.FCM,
    )


@pytest.fixture
def in_app_service(mock_session, mock_notification_repo, mock_log_repo) -> InAppService:
    return InAppService(
        session=mock_session,
        notification_repo=mock_notification_repo,
        log_repo=mock_log_repo,
    )


@pytest.fixture
def notification_service(mock_notification_repo, mock_queue_repo, mock_log_repo) -> NotificationService:
    return NotificationService(
        notification_repo=mock_notification_repo,
        queue_repo=mock_queue_repo,
        log_repo=mock_log_repo,
    )


@pytest.fixture
def template_service(mock_template_repo) -> TemplateService:
    return TemplateService(template_repo=mock_template_repo)


@pytest.fixture
def mock_notification_service_dep():
    """A mocked `NotificationService` for injection into `QueueService`,
    isolating queue-processing tests from the orchestrator's own logic."""
    return AsyncMock()


@pytest.fixture
def mock_email_dispatcher():
    dispatcher = AsyncMock()
    return dispatcher


@pytest.fixture
def retry_config() -> RetryConfig:
    return RetryConfig(
        max_retries=3, base_backoff_seconds=30, max_backoff_seconds=3600, backoff_multiplier=2.0
    )


@pytest.fixture
def queue_config() -> QueueConfig:
    return QueueConfig(batch_size=50, poll_interval_seconds=5, lock_timeout_seconds=300)


@pytest.fixture
def queue_service(
    mock_queue_repo,
    mock_notification_repo,
    mock_log_repo,
    mock_notification_service_dep,
    mock_email_dispatcher,
    retry_config,
    queue_config,
) -> QueueService:
    return QueueService(
        queue_repo=mock_queue_repo,
        notification_repo=mock_notification_repo,
        log_repo=mock_log_repo,
        notification_service=mock_notification_service_dep,
        dispatchers={NotificationChannel.EMAIL: mock_email_dispatcher},
        retry_config=retry_config,
        queue_config=queue_config,
    )


@pytest.fixture
def mock_queue_service_dep():
    """A mocked `QueueService` for injection into `SchedulerService`,
    isolating scheduling tests from queue-processing internals."""
    return AsyncMock()


@pytest.fixture
def scheduler_service(mock_notification_repo, mock_queue_service_dep, retry_config) -> SchedulerService:
    return SchedulerService(
        notification_repo=mock_notification_repo,
        queue_service=mock_queue_service_dep,
        retry_config=retry_config,
    )


def _make_email_detail(**overrides) -> MagicMock:
    detail = MagicMock()
    detail.to_email = overrides.get("to_email", "agent@realestateco.com")
    detail.from_email = overrides.get("from_email", "no-reply@realestateco.com")
    detail.subject = overrides.get("subject", "Lead Assigned")
    detail.html_body = overrides.get("html_body", None)
    detail.text_body = overrides.get("text_body", "A new lead has been assigned to you.")
    detail.cc = overrides.get("cc", None)
    detail.bcc = overrides.get("bcc", None)
    detail.reply_to = overrides.get("reply_to", None)
    return detail


def _make_notification(**overrides) -> MagicMock:
    obj = MagicMock()
    obj.id = overrides.get("id", uuid.uuid4())
    obj.status = overrides.get("status", NotificationStatus.PENDING)
    obj.retry_count = overrides.get("retry_count", 0)
    obj.max_retries = overrides.get("max_retries", 3)
    obj.is_read = overrides.get("is_read", False)
    obj.recipient_id = overrides.get("recipient_id", uuid.uuid4())
    obj.channel = overrides.get("channel", NotificationChannel.EMAIL)
    for key, value in overrides.items():
        setattr(obj, key, value)
    return obj


# --------------------------------------------------------------------------- #
# Email Service
# --------------------------------------------------------------------------- #

class TestEmailService:
    async def test_send_single_persists_notification_and_email_detail(
        self, email_service, mock_notification_repo, mock_log_repo, recipient_id
    ):
        mock_notification_repo.create.return_value = _make_notification(recipient_id=recipient_id)

        result = await email_service.send_single(
            recipient_id=recipient_id,
            to_email="agent@realestateco.com",
            subject="Lead Assigned",
            category=NotificationCategory.LEAD,
            text_body="A new lead has been assigned to you.",
        )

        mock_notification_repo.create.assert_awaited_once()
        assert mock_log_repo.create_log.await_count >= 1
        assert result is not None

    async def test_send_single_raises_when_no_body_provided(self, email_service, recipient_id):
        with pytest.raises(InvalidNotificationStateError):
            await email_service.send_single(
                recipient_id=recipient_id,
                to_email="agent@realestateco.com",
                subject="Lead Assigned",
                category=NotificationCategory.LEAD,
            )

    async def test_send_bulk_dispatches_all_recipients(
        self, email_service, mock_notification_repo, recipient_id
    ):
        mock_notification_repo.create.return_value = _make_notification(recipient_id=recipient_id)
        requests = [
            {
                "recipient_id": recipient_id,
                "to_email": f"agent{i}@realestateco.com",
                "subject": "Lead Assigned",
                "category": NotificationCategory.LEAD,
                "text_body": "You have a new lead.",
            }
            for i in range(3)
        ]

        results = await email_service.send_bulk(requests)

        assert len(results) == 3
        assert mock_notification_repo.create.await_count == 3

    async def test_dispatch_returns_success_result_from_provider(
        self, email_service, mock_email_provider
    ):
        notification = _make_notification()
        notification.email_detail = _make_email_detail()

        result = await email_service.dispatch(notification)

        mock_email_provider.send.assert_awaited_once()
        assert result.success is True
        assert result.provider_message_id == "smtp-msg-001"

    async def test_dispatch_raises_when_no_email_detail(self, email_service):
        notification = _make_notification()
        notification.email_detail = None

        with pytest.raises(InvalidNotificationStateError):
            await email_service.dispatch(notification)

    async def test_dispatch_raises_when_rate_limited(
        self, mock_session, mock_notification_repo, mock_log_repo, mock_email_provider
    ):
        rate_limiter = AsyncMock()
        rate_limiter.check.return_value = False
        service = EmailService(
            session=mock_session,
            notification_repo=mock_notification_repo,
            log_repo=mock_log_repo,
            provider=mock_email_provider,
            provider_name=EmailProvider.SMTP,
            default_sender_email="no-reply@realestateco.com",
            rate_limiter=rate_limiter,
        )
        notification = _make_notification()
        notification.email_detail = _make_email_detail()

        with pytest.raises(RateLimitExceededError):
            await service.dispatch(notification)
        mock_email_provider.send.assert_not_awaited()

    async def test_record_open_sets_opened_timestamp(self, email_service, mock_notification_repo):
        notification = _make_notification()
        notification.email_detail = _make_email_detail()
        mock_notification_repo.get_by_id.return_value = notification

        detail = await email_service.record_open(notification.id)

        assert detail.opened_at is not None

    async def test_record_open_raises_when_notification_missing(
        self, email_service, mock_notification_repo
    ):
        mock_notification_repo.get_by_id.return_value = None

        with pytest.raises(NotificationNotFoundError):
            await email_service.record_open(uuid.uuid4())


# --------------------------------------------------------------------------- #
# SMS Service
# --------------------------------------------------------------------------- #

class TestSMSService:
    async def test_send_single_persists_sms_detail(
        self, sms_service, mock_notification_repo, recipient_id
    ):
        mock_notification_repo.create.return_value = _make_notification(recipient_id=recipient_id)

        result = await sms_service.send_single(
            recipient_id=recipient_id,
            to_number="+14155552671",
            message_body="Your OTP is 4821",
            category=NotificationCategory.SYSTEM,
        )

        mock_notification_repo.create.assert_awaited_once()
        assert result is not None

    async def test_send_single_rejects_blank_message_body(self, sms_service, recipient_id):
        with pytest.raises(InvalidNotificationStateError):
            await sms_service.send_single(
                recipient_id=recipient_id,
                to_number="+14155552671",
                message_body="   ",
                category=NotificationCategory.SYSTEM,
            )

    async def test_send_single_rejects_message_exceeding_max_length(self, sms_service, recipient_id):
        with pytest.raises(InvalidNotificationStateError):
            await sms_service.send_single(
                recipient_id=recipient_id,
                to_number="+14155552671",
                message_body="x" * 1601,
                category=NotificationCategory.SYSTEM,
            )

    async def test_dispatch_marks_sent_status_on_success(self, sms_service, mock_sms_provider):
        notification = _make_notification()
        detail = MagicMock(
            to_number="+14155552671",
            from_number="+15550000000",
            message_body="Test",
            delivery_status=SMSDeliveryStatus.QUEUED,
        )
        notification.sms_detail = detail

        result = await sms_service.dispatch(notification)

        assert result.success is True
        assert detail.delivery_status == SMSDeliveryStatus.SENT

    async def test_dispatch_marks_failed_status_on_provider_failure(
        self, sms_service, mock_sms_provider
    ):
        mock_sms_provider.send.return_value = SMSSendResult(
            success=False, error_message="Carrier rejected message"
        )
        notification = _make_notification()
        detail = MagicMock(
            to_number="+14155552671",
            from_number="+15550000000",
            message_body="Test",
            delivery_status=SMSDeliveryStatus.QUEUED,
        )
        notification.sms_detail = detail

        result = await sms_service.dispatch(notification)

        assert result.success is False
        assert detail.delivery_status == SMSDeliveryStatus.FAILED


# --------------------------------------------------------------------------- #
# WhatsApp Service
# --------------------------------------------------------------------------- #

class TestWhatsAppService:
    async def test_send_single_persists_whatsapp_detail(
        self, whatsapp_service, mock_notification_repo, recipient_id
    ):
        mock_notification_repo.create.return_value = _make_notification(recipient_id=recipient_id)

        result = await whatsapp_service.send_single(
            recipient_id=recipient_id,
            to_number="+14155552671",
            message_type=WhatsAppMessageType.TEXT,
            category=NotificationCategory.APPOINTMENT,
            body="Your viewing is confirmed.",
        )

        mock_notification_repo.create.assert_awaited_once()
        assert result is not None

    async def test_dispatch_calls_provider_and_returns_message_id(
        self, whatsapp_service, mock_whatsapp_provider
    ):
        notification = _make_notification()
        notification.whatsapp_detail = MagicMock(
            to_number="+14155552671",
            from_number="+15551112222",
            message_type=WhatsAppMessageType.TEXT,
            text_body="Your viewing is confirmed.",
            template_name=None,
            template_language=None,
            template_parameters=None,
            media_url=None,
        )

        result = await whatsapp_service.dispatch(notification)

        mock_whatsapp_provider.send.assert_awaited_once()
        assert result.success is True
        assert result.provider_message_id == "wamid.HBg1"

    async def test_dispatch_raises_when_no_whatsapp_detail(self, whatsapp_service):
        notification = _make_notification()
        notification.whatsapp_detail = None

        with pytest.raises(InvalidNotificationStateError):
            await whatsapp_service.dispatch(notification)


# --------------------------------------------------------------------------- #
# Push Service
# --------------------------------------------------------------------------- #

class TestPushService:
    async def test_send_single_persists_push_detail(
        self, push_service, mock_notification_repo, recipient_id
    ):
        mock_notification_repo.create.return_value = _make_notification(recipient_id=recipient_id)

        result = await push_service.send_single(
            recipient_id=recipient_id,
            device_token="fcm-device-token-123",
            platform=DevicePlatform.ANDROID,
            title="New Lead",
            body="You have a new lead assigned.",
            category=NotificationCategory.LEAD,
        )

        mock_notification_repo.create.assert_awaited_once()
        assert result is not None

    async def test_dispatch_returns_dispatch_result_from_provider(
        self, push_service, mock_push_provider
    ):
        notification = _make_notification()
        notification.push_detail = MagicMock(
            device_token="fcm-device-token-123",
            platform=DevicePlatform.ANDROID,
            title="New Lead",
            body="Details",
            data_payload={"lead_id": "abc-123"},
            is_silent=False,
            badge_count=None,
        )

        result = await push_service.dispatch(notification)

        mock_push_provider.send.assert_awaited_once()
        assert result.success is True

    async def test_dispatch_raises_when_no_push_detail(self, push_service):
        notification = _make_notification()
        notification.push_detail = None

        with pytest.raises(InvalidNotificationStateError):
            await push_service.dispatch(notification)


# --------------------------------------------------------------------------- #
# In-App Service
# --------------------------------------------------------------------------- #

class TestInAppService:
    async def test_send_single_marks_delivered_immediately(
        self, in_app_service, mock_notification_repo, recipient_id
    ):
        created = _make_notification(
            recipient_id=recipient_id, status=NotificationStatus.DELIVERED
        )
        mock_notification_repo.create.return_value = created

        result = await in_app_service.send_single(
            recipient_id=recipient_id,
            user_id=recipient_id,
            title="New Message",
            body="You have a new message.",
            category=NotificationCategory.SYSTEM,
        )

        assert result.status == NotificationStatus.DELIVERED

    async def test_dispatch_returns_success_when_detail_present(self, in_app_service):
        notification = _make_notification()
        notification.in_app_detail = MagicMock()

        result = await in_app_service.dispatch(notification)

        assert result.success is True

    async def test_dispatch_raises_when_no_detail(self, in_app_service):
        notification = _make_notification()
        notification.in_app_detail = None

        with pytest.raises(InvalidNotificationStateError):
            await in_app_service.dispatch(notification)

    async def test_mark_read_updates_detail_and_notification(
        self, in_app_service, mock_notification_repo
    ):
        notification = _make_notification()
        detail = MagicMock(is_read=False, read_at=None)
        notification.in_app_detail = detail
        mock_notification_repo.get_by_id.return_value = notification

        result = await in_app_service.mark_read(notification.id)

        assert result.is_read is True
        mock_notification_repo.mark_as_read.assert_awaited_once()

    async def test_list_unread_for_user_filters_by_channel_and_read_state(
        self, in_app_service, mock_notification_repo, recipient_id
    ):
        mock_notification_repo.list_notifications.return_value = ([], 0)

        await in_app_service.list_unread_for_user(user_id=recipient_id)

        mock_notification_repo.list_notifications.assert_awaited_once_with(
            recipient_id=recipient_id,
            channel=NotificationChannel.IN_APP,
            is_read=False,
            page=1,
            page_size=20,
        )


# --------------------------------------------------------------------------- #
# Notification Service (Orchestrator)
# --------------------------------------------------------------------------- #

class TestNotificationServiceOrchestration:
    async def test_get_notification_raises_not_found(
        self, notification_service, mock_notification_repo
    ):
        mock_notification_repo.get_by_id.return_value = None
        with pytest.raises(NotificationNotFoundError):
            await notification_service.get_notification(uuid.uuid4())

    async def test_create_notification_succeeds_without_created_by_field(
    self,
    notification_service,
    mock_notification_repo,
    mock_queue_repo,
    mock_log_repo,
    recipient_id,
):

        """`create_notification` builds `Notification(**data, created_by=created_by)`,
        but the current `Notification` ORM model (see `app/models/notification.py`)
        defines no `created_by` column at all -- it isn't present directly, via
        `TimestampMixin`, or via `SoftDeleteMixin`. This is a genuine defect in the
        current production code, not a test-authoring gap, and per the strict
        "do not modify production code" scope for this rewrite it is documented
        here rather than silently patched around."""
        payload = NotificationCreate(
            recipient_id=recipient_id,
            channel=NotificationChannel.EMAIL,
            category=NotificationCategory.LEAD,
            priority=NotificationPriority.NORMAL,
            subject="Lead Assigned",
            body="A new lead has been assigned to you.",
        )

        created = _make_notification(
            recipient_id=recipient_id,
            channel=NotificationChannel.EMAIL,
            status=NotificationStatus.PENDING,
        )
        mock_notification_repo.create.return_value = created

        result = await notification_service.create_notification(
            payload, created_by=uuid.uuid4()
        )

        mock_notification_repo.create.assert_awaited_once()
        mock_queue_repo.enqueue.assert_awaited_once_with(created)
        mock_log_repo.create_log.assert_awaited_once()
        assert result is created

    async def test_retry_notification_requeues_failed_notification(
        self, notification_service, mock_notification_repo, mock_queue_repo
    ):
        target_id = uuid.uuid4()
        mock_notification_repo.get_by_id.return_value = _make_notification(
            id=target_id, status=NotificationStatus.FAILED, retry_count=1, max_retries=3
        )
        mock_notification_repo.update_fields.return_value = _make_notification(
            id=target_id, status=NotificationStatus.QUEUED, retry_count=2
        )

        result = await notification_service.retry_notification(target_id)

        mock_queue_repo.enqueue.assert_awaited_once()
        assert result.status == NotificationStatus.QUEUED

    async def test_retry_notification_raises_when_not_failed(
        self, notification_service, mock_notification_repo
    ):
        target_id = uuid.uuid4()
        mock_notification_repo.get_by_id.return_value = _make_notification(
            id=target_id, status=NotificationStatus.DELIVERED
        )
        with pytest.raises(InvalidNotificationStateError):
            await notification_service.retry_notification(target_id)

    async def test_retry_notification_raises_when_retry_limit_exceeded(
        self, notification_service, mock_notification_repo
    ):
        target_id = uuid.uuid4()
        mock_notification_repo.get_by_id.return_value = _make_notification(
            id=target_id, status=NotificationStatus.FAILED, retry_count=3, max_retries=3
        )
        with pytest.raises(RetryLimitExceededError):
            await notification_service.retry_notification(target_id)

    async def test_retry_notification_force_bypasses_retry_limit(
        self, notification_service, mock_notification_repo, mock_queue_repo
    ):
        target_id = uuid.uuid4()
        mock_notification_repo.get_by_id.return_value = _make_notification(
            id=target_id, status=NotificationStatus.FAILED, retry_count=3, max_retries=3
        )
        mock_notification_repo.update_fields.return_value = _make_notification(
            id=target_id, status=NotificationStatus.QUEUED, retry_count=4
        )

        result = await notification_service.retry_notification(target_id, force=True)

        assert result.status == NotificationStatus.QUEUED

    async def test_cancel_notification_marks_cancelled(
        self, notification_service, mock_notification_repo
    ):
        target_id = uuid.uuid4()
        mock_notification_repo.get_by_id.return_value = _make_notification(
            id=target_id, status=NotificationStatus.PENDING
        )
        mock_notification_repo.update_fields.return_value = _make_notification(
            id=target_id, status=NotificationStatus.CANCELLED
        )
        mock_notification_repo.get_by_id  # keep reference

        result = await notification_service.cancel_notification(target_id)

        assert result.status == NotificationStatus.CANCELLED

    async def test_cancel_notification_raises_for_terminal_status(
        self, notification_service, mock_notification_repo
    ):
        target_id = uuid.uuid4()
        mock_notification_repo.get_by_id.return_value = _make_notification(
            id=target_id, status=NotificationStatus.DELIVERED
        )
        with pytest.raises(InvalidNotificationStateError):
            await notification_service.cancel_notification(target_id)

    async def test_mark_as_read_is_idempotent(self, notification_service, mock_notification_repo):
        target_id = uuid.uuid4()
        already_read = _make_notification(id=target_id, is_read=True)
        mock_notification_repo.get_by_id.return_value = already_read

        result = await notification_service.mark_as_read(target_id)

        assert result is already_read
        mock_notification_repo.mark_as_read.assert_not_awaited()

    async def test_schedule_notification_rejects_past_timestamp(
        self, notification_service, recipient_id
    ):
        with pytest.raises(ValidationError):
            NotificationCreate(
                recipient_id=recipient_id,
                channel=NotificationChannel.EMAIL,
                category=NotificationCategory.LEAD,
                subject="Reminder",
                body="Past reminder",
                scheduled_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )


# --------------------------------------------------------------------------- #
# Bulk Notifications
# --------------------------------------------------------------------------- #

class TestBulkNotificationsService:
    async def test_bulk_send_accepts_all_recipients(
        self, notification_service, mock_notification_repo, mock_queue_repo
    ):
        recipients = [uuid.uuid4() for _ in range(3)]
        bulk_payload = BulkNotificationCreate(
            recipient_ids=recipients,
            channel=NotificationChannel.IN_APP,
            category=NotificationCategory.SYSTEM,
            priority=NotificationPriority.LOW,
            subject="System Maintenance",
            body="Scheduled maintenance tonight at 11 PM.",
        )

        created_items = [
            _make_notification(
                recipient_id=recipient_id,
                channel=NotificationChannel.IN_APP,
                status=NotificationStatus.PENDING,
            )
            for recipient_id in recipients
        ]
        mock_notification_repo.create.side_effect = created_items

        result = await notification_service.send_bulk(
            bulk_payload, created_by=uuid.uuid4()
        )

        assert result["accepted_count"] == 3
        assert result["rejected_count"] == 0
        assert result["accepted_ids"] == [item.id for item in created_items]
        assert result["rejected"] == []
        assert mock_notification_repo.create.await_count == 3
        assert mock_queue_repo.enqueue.await_count == 3

    async def test_bulk_send_rejects_empty_recipient_list_at_schema_level(self):
        with pytest.raises(ValidationError):
            BulkNotificationCreate(
                recipient_ids=[],
                channel=NotificationChannel.IN_APP,
                category=NotificationCategory.SYSTEM,
                priority=NotificationPriority.LOW,
                subject="Empty",
                body="Empty",
            )

    async def test_bulk_send_recipient_ids_are_deduplicated_at_schema_level(self):
        repeated = uuid.uuid4()
        payload = BulkNotificationCreate(
            recipient_ids=[repeated, repeated, uuid.uuid4()],
            channel=NotificationChannel.IN_APP,
            category=NotificationCategory.SYSTEM,
            priority=NotificationPriority.LOW,
            subject="Dedup test",
            body="Dedup test",
        )
        assert len(payload.recipient_ids) == 2


# --------------------------------------------------------------------------- #
# Delivery Tracking
# --------------------------------------------------------------------------- #

class TestDeliveryTracking:
    async def test_get_delivery_status_returns_current_state(
        self, notification_service, mock_notification_repo
    ):
        target_id = uuid.uuid4()
        mock_notification_repo.get_by_id.return_value = _make_notification(
            id=target_id, status=NotificationStatus.DELIVERED
        )
        status = await notification_service.get_delivery_status(target_id)
        assert status["status"] == NotificationStatus.DELIVERED

    async def test_get_delivery_status_returns_none_for_missing_notification(
        self, notification_service, mock_notification_repo
    ):
        mock_notification_repo.get_by_id.return_value = None
        status = await notification_service.get_delivery_status(uuid.uuid4())
        assert status is None

    async def test_get_statistics_delegates_to_repository(
        self, notification_service, mock_notification_repo
    ):
        mock_notification_repo.get_statistics.return_value = {
            "total": 100,
            "delivered": 82,
            "failed": 10,
            "pending": 8,
        }
        stats = await notification_service.get_statistics()
        assert stats["total"] == 100
        assert stats["delivered"] == 82

    async def test_record_delivery_success_logs_and_updates(
        self, notification_service, mock_notification_repo, mock_log_repo
    ):
        target_id = uuid.uuid4()
        mock_notification_repo.update_delivery_status.return_value = _make_notification(
            id=target_id, status=NotificationStatus.SENT, retry_count=0
        )

        result = await notification_service.record_delivery_success(target_id, "provider-msg-1")

        assert result.status == NotificationStatus.SENT
        mock_log_repo.create_log.assert_awaited_once()

    async def test_record_delivery_failure_raises_when_notification_missing(
        self, notification_service, mock_notification_repo
    ):
        mock_notification_repo.update_delivery_status.return_value = None
        with pytest.raises(NotificationNotFoundError):
            await notification_service.record_delivery_failure(
                uuid.uuid4(), "boom", attempt_number=1
            )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

class TestValidation:
    async def test_reject_empty_notification_body(self, recipient_id):
        with pytest.raises(ValidationError):
            NotificationCreate(
                recipient_id=recipient_id,
                channel=NotificationChannel.IN_APP,
                category=NotificationCategory.SYSTEM,
                priority=NotificationPriority.LOW,
                subject="Empty Body",
                body="   ",
            )

    async def test_reject_invalid_priority_value(self, recipient_id):
        with pytest.raises(ValidationError):
            NotificationCreate(
                recipient_id=recipient_id,
                channel=NotificationChannel.IN_APP,
                category=NotificationCategory.SYSTEM,
                priority="not_a_valid_priority",
                subject="Bad Priority",
                body="Body",
            )

    async def test_reject_naive_scheduled_at(self, recipient_id):
        with pytest.raises(ValidationError):
            NotificationCreate(
                recipient_id=recipient_id,
                channel=NotificationChannel.EMAIL,
                category=NotificationCategory.LEAD,
                subject="Reminder",
                body="Body",
                scheduled_at=datetime.now() + timedelta(hours=1),
            )

    async def test_list_notifications_rejects_non_positive_page(self, notification_service):
        with pytest.raises(InvalidNotificationStateError):
            await notification_service.list_notifications(page=0)


# --------------------------------------------------------------------------- #
# Business Rules
# --------------------------------------------------------------------------- #

class TestBusinessRules:
    async def test_update_notification_rejects_already_dispatched(
        self, notification_service, mock_notification_repo
    ):
        target_id = uuid.uuid4()
        mock_notification_repo.get_by_id.return_value = _make_notification(
            id=target_id, status=NotificationStatus.SENT
        )
        from app.schemas.notification import NotificationUpdate

        with pytest.raises(InvalidNotificationStateError):
            await notification_service.update_notification(
                target_id, NotificationUpdate(failure_reason="too late")
            )

    async def test_update_notification_returns_none_when_missing(
        self, notification_service, mock_notification_repo
    ):
        mock_notification_repo.get_by_id.return_value = None
        from app.schemas.notification import NotificationUpdate

        result = await notification_service.update_notification(
            uuid.uuid4(), NotificationUpdate(failure_reason="n/a")
        )
        assert result is None

    @pytest.mark.skip(
        reason=(
            "Debounce-window duplicate suppression does not exist anywhere in the "
            "current architecture: NotificationRepository has no "
            "exists_recent_duplicate() method, and NotificationService.create_notification() "
            "performs no duplicate-detection check before persisting. This behavior was "
            "removed/never ported when the notification module was rearchitected into "
            "per-channel services; there is no equivalent to adapt this test to."
        )
    )
    async def test_duplicate_notification_within_debounce_window_is_suppressed(self):
        ...

    @pytest.mark.skip(
        reason=(
            "Recipient channel-mute preferences do not exist anywhere in the current "
            "architecture: there is no get_recipient_preferences() method or muted-channel "
            "concept on NotificationService, NotificationRepository, or any channel service. "
            "This behavior was removed/never ported when the notification module was "
            "rearchitected into per-channel services; there is no equivalent to adapt this "
            "test to."
        )
    )
    async def test_muted_recipient_preference_suppresses_channel(self):
        ...


# --------------------------------------------------------------------------- #
# Template Service
# --------------------------------------------------------------------------- #

class TestTemplateService:
    async def test_create_template_uses_version_one_when_no_prior_active_template(
        self, template_service, mock_template_repo
    ):
        mock_template_repo.get_active_by_code.return_value = None
        mock_template_repo.create.return_value = MagicMock(id=uuid.uuid4(), code="lead_alert")

        payload = TemplateCreate(
            code="lead_alert",
            name="Lead Alert",
            channel=NotificationChannel.EMAIL,
            subject_template="New Lead: {{lead_name}}",
            body_template="{{lead_name}} is interested in {{property_name}}.",
            is_active=True,
        )

        result = await template_service.create_template(payload)

        mock_template_repo.create.assert_awaited_once()
        assert result.code == "lead_alert"

    async def test_create_template_increments_version_when_active_version_exists(
        self, template_service, mock_template_repo
    ):
        mock_template_repo.get_active_by_code.return_value = MagicMock(version=2)
        mock_template_repo.create.return_value = MagicMock(id=uuid.uuid4(), version=3)

        payload = TemplateCreate(
            code="lead_alert",
            name="Lead Alert",
            channel=NotificationChannel.EMAIL,
            subject_template="New Lead: {{lead_name}}",
            body_template="{{lead_name}} is interested.",
        )

        await template_service.create_template(payload)

        _, kwargs = mock_template_repo.create.call_args
        created_template = mock_template_repo.create.call_args[0][0]
        assert created_template.version == 3

    async def test_create_template_requires_code_field(self):
        with pytest.raises(ValidationError):
            TemplateCreate(
                name="broken",
                channel=NotificationChannel.EMAIL,
                subject_template="Subject",
                body_template="Body",
            )

    async def test_create_template_rejects_empty_body_at_schema_level(self):
        with pytest.raises(ValidationError):
            TemplateCreate(
                code="broken",
                name="broken",
                channel=NotificationChannel.EMAIL,
                subject_template="Subject",
                body_template="",
            )

    async def test_render_missing_placeholder_raises_template_render_error(
        self, template_service, mock_template_repo
    ):
        template = MagicMock(
            id=uuid.uuid4(),
            subject_template=None,
            body_template="Hello {{name}}",
            variables={},
        )
        mock_template_repo.get_active_by_code.return_value = template

        with pytest.raises(TemplateRenderError):
            await template_service.render(
                code="greeting", channel=NotificationChannel.EMAIL, variables={}
            )

    async def test_render_raises_when_required_variable_missing(
        self, template_service, mock_template_repo
    ):
        template = MagicMock(
            id=uuid.uuid4(),
            subject_template=None,
            body_template="Hello {{name}}",
            variables={"name": "string"},
        )
        mock_template_repo.get_active_by_code.return_value = template

        with pytest.raises(TemplateRenderError):
            await template_service.render(
                code="greeting", channel=NotificationChannel.EMAIL, variables={}
            )

    async def test_render_succeeds_with_all_variables_present(
        self, template_service, mock_template_repo
    ):
        template = MagicMock(
            id=uuid.uuid4(),
            subject_template="Hi {{name}}",
            body_template="Hello {{name}}, welcome!",
            variables={"name": "string"},
        )
        mock_template_repo.get_active_by_code.return_value = template

        rendered = await template_service.render(
            code="greeting", channel=NotificationChannel.EMAIL, variables={"name": "Priya"}
        )

        assert rendered.subject == "Hi Priya"
        assert rendered.body == "Hello Priya, welcome!"

    async def test_get_active_template_excludes_inactive_via_render(
        self, template_service, mock_template_repo
    ):
        """`TemplateService` has no standalone `get_active()` method. Active
        template resolution happens through `template_repo.get_active_by_code()`,
        which by contract only ever returns an active template version. This
        test exercises that same resolution path via `render()`, which is the
        real current API for resolving "the active template for a code" --
        simulating the repository correctly reporting no active template
        (e.g. because the only stored version is inactive)."""
        mock_template_repo.get_active_by_code.return_value = None

        with pytest.raises(ServiceTemplateNotFoundError):
            await template_service.render(
                code="inactive_one", channel=NotificationChannel.EMAIL, variables={}
            )


# --------------------------------------------------------------------------- #
# Queue Service
# --------------------------------------------------------------------------- #

class TestQueueService:
    async def test_enqueue_notification_creates_queue_entry_and_marks_queued(
        self, queue_service, mock_queue_repo, mock_notification_repo, mock_log_repo
    ):
        notification = _make_notification(priority=NotificationPriority.HIGH)
        notification.scheduled_at = None
        notification.max_retries = 3
        mock_queue_repo.enqueue.return_value = MagicMock(
            id=uuid.uuid4(), notification_id=notification.id, priority=NotificationPriority.HIGH
        )

        result = await queue_service.enqueue_notification(notification)

        mock_queue_repo.enqueue.assert_awaited_once()
        mock_notification_repo.update_fields.assert_awaited_once_with(
            notification.id, {"status": NotificationStatus.QUEUED}
        )
        assert result.priority == NotificationPriority.HIGH

    async def test_bulk_enqueue_creates_entries_for_all_notifications(
        self, queue_service, mock_queue_repo, mock_notification_repo
    ):
        notifications = [_make_notification() for _ in range(3)]
        for n in notifications:
            n.scheduled_at = None
            n.max_retries = 3
        mock_queue_repo.bulk_enqueue.return_value = [MagicMock() for _ in notifications]

        result = await queue_service.bulk_enqueue(notifications)

        assert len(result) == 3
        mock_notification_repo.bulk_update_status.assert_awaited_once()

    async def test_process_next_batch_marks_completed_on_successful_dispatch(
        self, queue_service, mock_queue_repo, mock_notification_repo, mock_notification_service_dep,
        mock_email_dispatcher,
    ):
        from app.services.notification_service import DispatchResult

        queue_entry = MagicMock(
            id=uuid.uuid4(), notification_id=uuid.uuid4(), retry_count=0, max_retries=3
        )
        mock_queue_repo.fetch_next_batch.return_value = [queue_entry]
        notification = _make_notification(
            id=queue_entry.notification_id, channel=NotificationChannel.EMAIL
        )
        mock_notification_repo.get_by_id.return_value = notification
        mock_email_dispatcher.dispatch.return_value = DispatchResult(
            success=True, provider_message_id="msg-1"
        )

        result = await queue_service.process_next_batch(worker_id="worker-1")

        assert result.claimed == 1
        assert result.succeeded == 1
        mock_queue_repo.mark_completed.assert_awaited_once_with(queue_entry.id)
        mock_notification_service_dep.record_delivery_success.assert_awaited_once()

    async def test_process_next_batch_retries_when_dispatch_fails_and_retries_remain(
        self, queue_service, mock_queue_repo, mock_notification_repo, mock_email_dispatcher,
    ):
        from app.services.notification_service import DispatchResult

        queue_entry = MagicMock(
            id=uuid.uuid4(), notification_id=uuid.uuid4(), retry_count=0, max_retries=3
        )
        mock_queue_repo.fetch_next_batch.return_value = [queue_entry]
        notification = _make_notification(
            id=queue_entry.notification_id, channel=NotificationChannel.EMAIL
        )
        mock_notification_repo.get_by_id.return_value = notification
        mock_email_dispatcher.dispatch.return_value = DispatchResult(
            success=False, error_message="temporary failure"
        )

        result = await queue_service.process_next_batch(worker_id="worker-1")

        assert result.retried == 1
        mock_queue_repo.schedule_retry.assert_awaited_once()

    async def test_process_next_batch_marks_failed_when_retries_exhausted(
        self, queue_service, mock_queue_repo, mock_notification_repo, mock_email_dispatcher,
        mock_notification_service_dep,
    ):
        from app.services.notification_service import DispatchResult

        queue_entry = MagicMock(
            id=uuid.uuid4(), notification_id=uuid.uuid4(), retry_count=3, max_retries=3
        )
        mock_queue_repo.fetch_next_batch.return_value = [queue_entry]
        notification = _make_notification(
            id=queue_entry.notification_id, channel=NotificationChannel.EMAIL
        )
        mock_notification_repo.get_by_id.return_value = notification
        mock_email_dispatcher.dispatch.return_value = DispatchResult(
            success=False, error_message="permanent failure"
        )

        result = await queue_service.process_next_batch(worker_id="worker-1")

        assert result.failed == 1
        mock_queue_repo.mark_failed.assert_awaited_once()
        mock_notification_service_dep.record_delivery_failure.assert_awaited_once()

    async def test_process_next_batch_returns_zero_claimed_when_queue_empty(
        self, queue_service, mock_queue_repo
    ):
        mock_queue_repo.fetch_next_batch.return_value = []

        result = await queue_service.process_next_batch(worker_id="worker-1")

        assert result == BatchProcessResult(claimed=0, succeeded=0, failed=0, retried=0)

    async def test_retry_now_raises_when_retry_limit_exceeded(
        self, queue_service, mock_queue_repo
    ):
        target_id = uuid.uuid4()
        mock_queue_repo.get_by_notification_id.return_value = MagicMock(
            id=uuid.uuid4(), retry_count=3, max_retries=3
        )
        with pytest.raises(RetryLimitExceededError):
            await queue_service.retry_now(target_id)

    async def test_retry_now_raises_not_found_when_no_queue_entry(
        self, queue_service, mock_queue_repo
    ):
        mock_queue_repo.get_by_notification_id.return_value = None
        with pytest.raises(NotificationNotFoundError):
            await queue_service.retry_now(uuid.uuid4())

    async def test_cancel_raises_not_found_when_notification_missing(
        self, queue_service, mock_queue_repo, mock_notification_repo
    ):
        mock_queue_repo.get_by_notification_id.return_value = None
        mock_notification_repo.update_fields.return_value = None
        with pytest.raises(NotificationNotFoundError):
            await queue_service.cancel(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Scheduler Service
# --------------------------------------------------------------------------- #

class TestSchedulerService:
    async def test_schedule_notification_for_future_time_enqueues(
        self, scheduler_service, mock_notification_repo, mock_queue_service_dep
    ):
        target_id = uuid.uuid4()
        scheduled_time = datetime.now(timezone.utc) + timedelta(hours=2)
        mock_notification_repo.get_by_id.return_value = _make_notification(id=target_id)
        mock_notification_repo.update_fields.return_value = _make_notification(
            id=target_id, scheduled_at=scheduled_time
        )

        result = await scheduler_service.schedule_notification(target_id, scheduled_time)

        mock_queue_service_dep.enqueue_notification.assert_awaited_once()
        assert result.scheduled_at == scheduled_time

    async def test_schedule_notification_rejects_naive_timestamp(self, scheduler_service):
        with pytest.raises(SchedulingError):
            await scheduler_service.schedule_notification(
                uuid.uuid4(), datetime.now() + timedelta(hours=1)
            )

    async def test_schedule_notification_rejects_past_timestamp(self, scheduler_service):
        with pytest.raises(SchedulingError):
            await scheduler_service.schedule_notification(
                uuid.uuid4(), datetime.now(timezone.utc) - timedelta(hours=1)
            )

    async def test_schedule_notification_raises_not_found(
        self, scheduler_service, mock_notification_repo
    ):
        mock_notification_repo.get_by_id.return_value = None
        with pytest.raises(NotificationNotFoundError):
            await scheduler_service.schedule_notification(
                uuid.uuid4(), datetime.now(timezone.utc) + timedelta(hours=1)
            )

    async def test_reschedule_notification_moves_dispatch_time(
        self, scheduler_service, mock_notification_repo, mock_queue_service_dep
    ):
        target_id = uuid.uuid4()
        new_time = datetime.now(timezone.utc) + timedelta(hours=5)
        mock_notification_repo.get_by_id.return_value = _make_notification(
            id=target_id, scheduled_at=new_time
        )

        result = await scheduler_service.reschedule_notification(target_id, new_time)

        mock_queue_service_dep.reschedule.assert_awaited_once_with(target_id, new_time)
        assert result.scheduled_at == new_time

    async def test_cancel_schedule_delegates_to_queue_service(
        self, scheduler_service, mock_queue_service_dep
    ):
        target_id = uuid.uuid4()
        await scheduler_service.cancel_schedule(target_id)
        mock_queue_service_dep.cancel.assert_awaited_once_with(target_id)

    async def test_process_due_notifications_delegates_to_queue_service(
        self, scheduler_service, mock_queue_service_dep
    ):
        mock_queue_service_dep.process_next_batch.return_value = BatchProcessResult(
            claimed=3, succeeded=3, failed=0, retried=0
        )

        result = await scheduler_service.process_due_notifications(worker_id="worker-1")

        assert result.claimed == 3
        mock_queue_service_dep.process_next_batch.assert_awaited_once_with("worker-1")

    async def test_release_expired_locks_delegates_to_queue_service(
        self, scheduler_service, mock_queue_service_dep
    ):
        mock_queue_service_dep.release_stale_locks.return_value = 2
        result = await scheduler_service.release_expired_locks()
        assert result == 2


# --------------------------------------------------------------------------- #
# Retry Logic
# --------------------------------------------------------------------------- #

class TestRetryLogic:
    async def test_retry_uses_exponential_backoff_delay(self, retry_config):
        delay_1 = retry_config.compute_backoff_seconds(1)
        delay_2 = retry_config.compute_backoff_seconds(2)
        delay_3 = retry_config.compute_backoff_seconds(3)

        assert delay_2 > delay_1
        assert delay_3 > delay_2

    async def test_backoff_delay_is_capped_at_max_backoff_seconds(self):
        config = RetryConfig(
            max_retries=10,
            base_backoff_seconds=1000,
            max_backoff_seconds=1500,
            backoff_multiplier=5.0,
        )
        delay = config.compute_backoff_seconds(10)
        assert delay == 1500

    async def test_retry_notification_increments_retry_count(
        self, notification_service, mock_notification_repo
    ):
        target_id = uuid.uuid4()
        mock_notification_repo.get_by_id.return_value = _make_notification(
            id=target_id, status=NotificationStatus.FAILED, retry_count=1, max_retries=5
        )
        mock_notification_repo.update_fields.return_value = _make_notification(
            id=target_id, status=NotificationStatus.QUEUED, retry_count=2
        )

        result = await notification_service.retry_notification(target_id)

        mock_notification_repo.update_fields.assert_awaited_once_with(
            target_id, {"status": NotificationStatus.QUEUED, "retry_count": 2}
        )
        assert result.retry_count == 2

    async def test_retry_notification_returns_none_when_missing(
        self, notification_service, mock_notification_repo
    ):
        mock_notification_repo.get_by_id.return_value = None
        result = await notification_service.retry_notification(uuid.uuid4())
        assert result is None
