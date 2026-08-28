# backend/app/core/notification_settings.py
"""Centralized configuration for the notification module.

Defines retry, queue, and priority configuration surfaced through
environment-backed settings objects.
"""

from functools import lru_cache
from typing import Dict

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.notification import NotificationPriority


class RetryConfig(BaseSettings):
    """Retry policy configuration for notification dispatch attempts.

    Attributes:
        max_retries: Default maximum number of delivery attempts.
        base_backoff_seconds: Base delay used for exponential backoff.
        max_backoff_seconds: Upper bound applied to computed backoff delays.
        backoff_multiplier: Multiplier applied per retry attempt.
    """

    model_config = SettingsConfigDict(env_prefix="NOTIFICATION_RETRY_")

    max_retries: int = Field(default=3, ge=0)
    base_backoff_seconds: int = Field(default=30, gt=0)
    max_backoff_seconds: int = Field(default=3600, gt=0)
    backoff_multiplier: float = Field(default=2.0, gt=1.0)

    def compute_backoff_seconds(self, attempt_number: int) -> int:
        """Compute the backoff delay for a given retry attempt.

        Args:
            attempt_number: 1-indexed retry attempt number.

        Returns:
            Delay in seconds before the next retry attempt, capped at
            `max_backoff_seconds`.
        """
        delay = self.base_backoff_seconds * (self.backoff_multiplier ** max(attempt_number - 1, 0))
        return min(int(delay), self.max_backoff_seconds)


class QueueConfig(BaseSettings):
    """Queue processing configuration.

    Attributes:
        batch_size: Number of queue entries claimed per worker poll cycle.
        poll_interval_seconds: Delay between successive queue poll cycles.
        lock_timeout_seconds: Duration after which a processing lock is
            considered stale and eligible for release.
        stale_lock_check_interval_seconds: Delay between successive stale
            lock reclamation sweeps.
    """

    model_config = SettingsConfigDict(env_prefix="NOTIFICATION_QUEUE_")

    batch_size: int = Field(default=50, gt=0)
    poll_interval_seconds: int = Field(default=5, gt=0)
    lock_timeout_seconds: int = Field(default=300, gt=0)
    stale_lock_check_interval_seconds: int = Field(default=60, gt=0)


class PriorityConfig(BaseSettings):
    """Priority weighting configuration used for queue ordering.

    Attributes:
        weights: Mapping of priority level to numeric ordering weight,
            where lower values are processed first.
    """

    model_config = SettingsConfigDict(env_prefix="NOTIFICATION_PRIORITY_")

    weights: Dict[NotificationPriority, int] = Field(
        default_factory=lambda: {
            NotificationPriority.URGENT: 0,
            NotificationPriority.HIGH: 1,
            NotificationPriority.NORMAL: 2,
            NotificationPriority.LOW: 3,
        }
    )

    def weight_for(self, priority: NotificationPriority) -> int:
        """Resolve the ordering weight for a given priority level.

        Args:
            priority: Priority level to resolve.

        Returns:
            The numeric ordering weight, defaulting to the normal
            priority weight if unmapped.
        """
        return self.weights.get(priority, self.weights[NotificationPriority.NORMAL])


class NotificationSettings(BaseSettings):
    """Aggregate configuration root for the notification module.

    Attributes:
        retry: Retry policy configuration.
        queue: Queue processing configuration.
        priority: Priority weighting configuration.
        default_sender_email: Default from-address for outbound email.
        default_sender_sms_number: Default from-number for outbound SMS.
        default_whatsapp_business_number: Default from-number for
            outbound WhatsApp messages.
    """

    model_config = SettingsConfigDict(
        env_prefix="NOTIFICATION_", env_file=".env", extra="ignore"
    )

    retry: RetryConfig = Field(default_factory=RetryConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    priority: PriorityConfig = Field(default_factory=PriorityConfig)
    default_sender_email: str = Field(default="no-reply@example.com")
    default_sender_sms_number: str = Field(default="")
    default_whatsapp_business_number: str = Field(default="")


@lru_cache
def get_notification_settings() -> NotificationSettings:
    """Return a cached singleton instance of the notification settings.

    Returns:
        The process-wide `NotificationSettings` instance.
    """
    return NotificationSettings()