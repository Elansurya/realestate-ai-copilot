# backend/app/core/sms_provider.py
"""SMS provider interfaces and factory.

Defines the transport-agnostic contract for sending SMS messages and
concrete provider implementations selectable via a factory.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

from app.models.sms_notification import SMSProvider


@dataclass
class SMSMessage:
    """Transport-agnostic SMS message payload.

    Attributes:
        from_number: Sender phone number in E.164 format.
        to_number: Recipient phone number in E.164 format.
        body: SMS text content.
    """

    from_number: str
    to_number: str
    body: str


@dataclass
class SMSSendResult:
    """Result of an SMS dispatch attempt.

    Attributes:
        success: Whether the message was accepted by the provider.
        provider_message_id: Provider-assigned message identifier, if any.
        error_message: Error detail when `success` is False.
    """

    success: bool
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None


class SMSProviderInterface(ABC):
    """Abstract contract for SMS delivery providers."""

    @abstractmethod
    async def send(self, message: SMSMessage) -> SMSSendResult:
        """Send an SMS message.

        Args:
            message: SMS message to dispatch.

        Returns:
            The outcome of the dispatch attempt.
        """
        raise NotImplementedError


@dataclass
class TwilioConfig:
    """Configuration required to authenticate with the Twilio REST API.

    Attributes:
        account_sid: Twilio account identifier.
        auth_token: Twilio authentication token.
        request_timeout_seconds: HTTP request timeout in seconds.
    """

    account_sid: str
    auth_token: str
    request_timeout_seconds: float = 10.0


class TwilioSMSProvider(SMSProviderInterface):
    """Twilio-based SMS provider implementation.

    Attributes:
        config: Twilio API credentials and connection settings.
    """

    _API_BASE_URL = "https://api.twilio.com/2010-04-01"

    def __init__(self, config: TwilioConfig) -> None:
        """Initialize the Twilio provider.

        Args:
            config: Twilio API credentials and connection settings.
        """
        self.config = config

    async def send(self, message: SMSMessage) -> SMSSendResult:
        """Send an SMS message through the Twilio REST API.

        Args:
            message: SMS message to dispatch.

        Returns:
            The outcome of the dispatch attempt.
        """
        url = f"{self._API_BASE_URL}/Accounts/{self.config.account_sid}/Messages.json"
        payload = {
            "From": message.from_number,
            "To": message.to_number,
            "Body": message.body,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.config.request_timeout_seconds
            ) as client:
                response = await client.post(
                    url,
                    data=payload,
                    auth=(self.config.account_sid, self.config.auth_token),
                )
            response.raise_for_status()
            data = response.json()
            return SMSSendResult(success=True, provider_message_id=data.get("sid"))
        except httpx.HTTPError as exc:
            return SMSSendResult(success=False, error_message=str(exc))


@dataclass
class MSG91Config:
    """Configuration required to authenticate with the MSG91 API.

    Attributes:
        auth_key: MSG91 API authentication key.
        sender_id: Registered sender identifier.
        route: MSG91 message routing category.
        request_timeout_seconds: HTTP request timeout in seconds.
    """

    auth_key: str
    sender_id: str
    route: str = "4"
    request_timeout_seconds: float = 10.0


class MSG91SMSProvider(SMSProviderInterface):
    """MSG91-based SMS provider implementation.

    Attributes:
        config: MSG91 API credentials and connection settings.
    """

    _API_URL = "https://api.msg91.com/api/v5/flow/"

    def __init__(self, config: MSG91Config) -> None:
        """Initialize the MSG91 provider.

        Args:
            config: MSG91 API credentials and connection settings.
        """
        self.config = config

    async def send(self, message: SMSMessage) -> SMSSendResult:
        """Send an SMS message through the MSG91 API.

        Args:
            message: SMS message to dispatch.

        Returns:
            The outcome of the dispatch attempt.
        """
        headers = {"authkey": self.config.auth_key, "Content-Type": "application/json"}
        payload = {
            "sender": self.config.sender_id,
            "route": self.config.route,
            "mobiles": message.to_number,
            "message": message.body,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.config.request_timeout_seconds
            ) as client:
                response = await client.post(self._API_URL, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            return SMSSendResult(
                success=True, provider_message_id=data.get("request_id")
            )
        except httpx.HTTPError as exc:
            return SMSSendResult(success=False, error_message=str(exc))


class SMSProviderFactory:
    """Factory that resolves the appropriate SMS provider implementation."""

    @staticmethod
    def create(
        provider: SMSProvider,
        twilio_config: Optional[TwilioConfig] = None,
        msg91_config: Optional[MSG91Config] = None,
    ) -> SMSProviderInterface:
        """Instantiate an SMS provider implementation.

        Args:
            provider: Identifier of the provider to instantiate.
            twilio_config: Twilio configuration, required when `provider`
                is `SMSProvider.TWILIO`.
            msg91_config: MSG91 configuration, required when `provider` is
                `SMSProvider.MSG91`.

        Returns:
            A concrete `SMSProviderInterface` implementation.

        Raises:
            ValueError: If the requested provider is unsupported or its
                required configuration is missing.
        """
        if provider == SMSProvider.TWILIO:
            if twilio_config is None:
                raise ValueError("twilio_config is required for the Twilio provider")
            return TwilioSMSProvider(twilio_config)
        if provider == SMSProvider.MSG91:
            if msg91_config is None:
                raise ValueError("msg91_config is required for the MSG91 provider")
            return MSG91SMSProvider(msg91_config)
        raise ValueError(f"unsupported sms provider: {provider}")