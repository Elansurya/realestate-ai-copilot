# backend/app/core/push_provider.py
"""Push notification provider interfaces and factory.

Defines the transport-agnostic contract for sending push notifications and
concrete provider implementations selectable via a factory.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx

from app.models.push_notification import DevicePlatform, PushProvider


@dataclass
class PushMessage:
    """Transport-agnostic push notification payload.

    Attributes:
        device_token: Target device push token/registration id.
        platform: Target device platform.
        title: Push notification title.
        body: Push notification body text.
        data_payload: Optional custom data payload delivered with the push.
        is_silent: Whether this is a silent/background push.
        badge_count: App icon badge count to set, if applicable.
    """

    device_token: str
    platform: DevicePlatform
    title: str
    body: str
    data_payload: Dict[str, Any] = field(default_factory=dict)
    is_silent: bool = False
    badge_count: Optional[int] = None


@dataclass
class PushSendResult:
    """Result of a push dispatch attempt.

    Attributes:
        success: Whether the message was accepted by the provider.
        provider_message_id: Provider-assigned message identifier, if any.
        error_message: Error detail when `success` is False.
    """

    success: bool
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None


class PushProviderInterface(ABC):
    """Abstract contract for push notification delivery providers."""

    @abstractmethod
    async def send(self, message: PushMessage) -> PushSendResult:
        """Send a push notification.

        Args:
            message: Push message to dispatch.

        Returns:
            The outcome of the dispatch attempt.
        """
        raise NotImplementedError


@dataclass
class FCMConfig:
    """Configuration required to authenticate with Firebase Cloud Messaging.

    Attributes:
        project_id: Firebase project identifier.
        access_token: OAuth2 bearer token authorized for the FCM HTTP v1 API.
        request_timeout_seconds: HTTP request timeout in seconds.
    """

    project_id: str
    access_token: str
    request_timeout_seconds: float = 10.0


class FCMPushProvider(PushProviderInterface):
    """Firebase Cloud Messaging-based push provider implementation.

    Attributes:
        config: FCM credentials and connection settings.
    """

    def __init__(self, config: FCMConfig) -> None:
        """Initialize the FCM provider.

        Args:
            config: FCM credentials and connection settings.
        """
        self.config = config

    async def send(self, message: PushMessage) -> PushSendResult:
        """Send a push notification through the FCM HTTP v1 API.

        Args:
            message: Push message to dispatch.

        Returns:
            The outcome of the dispatch attempt.
        """
        url = (
            f"https://fcm.googleapis.com/v1/projects/"
            f"{self.config.project_id}/messages:send"
        )
        headers = {
            "Authorization": f"Bearer {self.config.access_token}",
            "Content-Type": "application/json",
        }
        notification_block = None
        if not message.is_silent:
            notification_block = {"title": message.title, "body": message.body}
        payload_message: Dict[str, Any] = {
            "token": message.device_token,
            "data": {str(k): str(v) for k, v in message.data_payload.items()},
        }
        if notification_block:
            payload_message["notification"] = notification_block
        if message.badge_count is not None:
            payload_message["apns"] = {
                "payload": {"aps": {"badge": message.badge_count}}
            }
        try:
            async with httpx.AsyncClient(
                timeout=self.config.request_timeout_seconds
            ) as client:
                response = await client.post(
                    url, json={"message": payload_message}, headers=headers
                )
            response.raise_for_status()
            data = response.json()
            return PushSendResult(success=True, provider_message_id=data.get("name"))
        except httpx.HTTPError as exc:
            return PushSendResult(success=False, error_message=str(exc))


class PushProviderFactory:
    """Factory that resolves the appropriate push provider implementation."""

    @staticmethod
    def create(
        provider: PushProvider, fcm_config: Optional[FCMConfig] = None
    ) -> PushProviderInterface:
        """Instantiate a push provider implementation.

        Args:
            provider: Identifier of the provider to instantiate.
            fcm_config: FCM configuration, required when `provider` is
                `PushProvider.FCM`.

        Returns:
            A concrete `PushProviderInterface` implementation.

        Raises:
            ValueError: If the requested provider is unsupported or its
                required configuration is missing.
        """
        if provider == PushProvider.FCM:
            if fcm_config is None:
                raise ValueError("fcm_config is required for the FCM provider")
            return FCMPushProvider(fcm_config)
        raise ValueError(f"unsupported push provider: {provider}")