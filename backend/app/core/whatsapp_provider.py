# backend/app/core/whatsapp_provider.py
"""WhatsApp provider interfaces and factory.

Defines the transport-agnostic contract for sending WhatsApp messages and
concrete provider implementations selectable via a factory.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

from app.models.whatsapp_notification import WhatsAppMessageType, WhatsAppProvider


@dataclass
class WhatsAppMessage:
    """Transport-agnostic WhatsApp message payload.

    Attributes:
        from_number: Sender WhatsApp business number in E.164 format.
        to_number: Recipient WhatsApp number in E.164 format.
        message_type: Type of WhatsApp message payload.
        text_body: Plain text content, used for text messages.
        template_name: Approved template name, required for template messages.
        template_language: Language code of the approved template.
        template_parameters: Ordered parameter values for template placeholders.
        media_url: Media asset URL, required for media messages.
    """

    from_number: str
    to_number: str
    message_type: WhatsAppMessageType
    text_body: Optional[str] = None
    template_name: Optional[str] = None
    template_language: Optional[str] = None
    template_parameters: Optional[list] = None
    media_url: Optional[str] = None


@dataclass
class WhatsAppSendResult:
    """Result of a WhatsApp dispatch attempt.

    Attributes:
        success: Whether the message was accepted by the provider.
        provider_message_id: Provider-assigned message identifier, if any.
        error_message: Error detail when `success` is False.
    """

    success: bool
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None


class WhatsAppProviderInterface(ABC):
    """Abstract contract for WhatsApp delivery providers."""

    @abstractmethod
    async def send(self, message: WhatsAppMessage) -> WhatsAppSendResult:
        """Send a WhatsApp message.

        Args:
            message: WhatsApp message to dispatch.

        Returns:
            The outcome of the dispatch attempt.
        """
        raise NotImplementedError


@dataclass
class MetaCloudAPIConfig:
    """Configuration required to authenticate with the Meta WhatsApp Cloud API.

    Attributes:
        phone_number_id: Meta-assigned phone number identifier.
        access_token: Meta Graph API access token.
        api_version: Graph API version segment, e.g. "v19.0".
        request_timeout_seconds: HTTP request timeout in seconds.
    """

    phone_number_id: str
    access_token: str
    api_version: str = "v19.0"
    request_timeout_seconds: float = 10.0


class MetaCloudAPIWhatsAppProvider(WhatsAppProviderInterface):
    """Meta WhatsApp Cloud API-based provider implementation.

    Attributes:
        config: Meta Cloud API credentials and connection settings.
    """

    def __init__(self, config: MetaCloudAPIConfig) -> None:
        """Initialize the Meta Cloud API provider.

        Args:
            config: Meta Cloud API credentials and connection settings.
        """
        self.config = config

    def _build_payload(self, message: WhatsAppMessage) -> dict:
        """Construct the Graph API request payload for a message.

        Args:
            message: WhatsApp message to convert.

        Returns:
            A dictionary matching the Graph API message schema.

        Raises:
            ValueError: If required fields for the message type are missing.
        """
        base_payload = {
            "messaging_product": "whatsapp",
            "to": message.to_number,
        }
        if message.message_type == WhatsAppMessageType.TEXT:
            if not message.text_body:
                raise ValueError("text_body is required for text messages")
            base_payload["type"] = "text"
            base_payload["text"] = {"body": message.text_body}
        elif message.message_type == WhatsAppMessageType.TEMPLATE:
            if not message.template_name:
                raise ValueError("template_name is required for template messages")
            components = []
            if message.template_parameters:
                components.append(
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(param)}
                            for param in message.template_parameters
                        ],
                    }
                )
            base_payload["type"] = "template"
            base_payload["template"] = {
                "name": message.template_name,
                "language": {"code": message.template_language or "en_US"},
                "components": components,
            }
        elif message.message_type == WhatsAppMessageType.MEDIA:
            if not message.media_url:
                raise ValueError("media_url is required for media messages")
            base_payload["type"] = "image"
            base_payload["image"] = {"link": message.media_url}
        else:
            raise ValueError(f"unsupported message type: {message.message_type}")
        return base_payload

    async def send(self, message: WhatsAppMessage) -> WhatsAppSendResult:
        """Send a WhatsApp message through the Meta Cloud API.

        Args:
            message: WhatsApp message to dispatch.

        Returns:
            The outcome of the dispatch attempt.
        """
        url = (
            f"https://graph.facebook.com/{self.config.api_version}/"
            f"{self.config.phone_number_id}/messages"
        )
        headers = {
            "Authorization": f"Bearer {self.config.access_token}",
            "Content-Type": "application/json",
        }
        try:
            payload = self._build_payload(message)
        except ValueError as exc:
            return WhatsAppSendResult(success=False, error_message=str(exc))

        try:
            async with httpx.AsyncClient(
                timeout=self.config.request_timeout_seconds
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            message_id = None
            messages = data.get("messages")
            if messages:
                message_id = messages[0].get("id")
            return WhatsAppSendResult(success=True, provider_message_id=message_id)
        except httpx.HTTPError as exc:
            return WhatsAppSendResult(success=False, error_message=str(exc))


class WhatsAppProviderFactory:
    """Factory that resolves the appropriate WhatsApp provider implementation."""

    @staticmethod
    def create(
        provider: WhatsAppProvider,
        meta_cloud_config: Optional[MetaCloudAPIConfig] = None,
    ) -> WhatsAppProviderInterface:
        """Instantiate a WhatsApp provider implementation.

        Args:
            provider: Identifier of the provider to instantiate.
            meta_cloud_config: Meta Cloud API configuration, required when
                `provider` is `WhatsAppProvider.META_CLOUD_API`.

        Returns:
            A concrete `WhatsAppProviderInterface` implementation.

        Raises:
            ValueError: If the requested provider is unsupported or its
                required configuration is missing.
        """
        if provider == WhatsAppProvider.META_CLOUD_API:
            if meta_cloud_config is None:
                raise ValueError(
                    "meta_cloud_config is required for the Meta Cloud API provider"
                )
            return MetaCloudAPIWhatsAppProvider(meta_cloud_config)
        raise ValueError(f"unsupported whatsapp provider: {provider}")