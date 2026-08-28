# backend/app/core/email_provider.py
"""Email provider interfaces and factory.

Defines the transport-agnostic contract for sending email messages and
concrete provider implementations selectable via a factory.
"""

import smtplib
import ssl
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import partial
from typing import List, Optional

import anyio

from app.models.email_notification import EmailProvider


@dataclass
class EmailMessage:
    """Transport-agnostic email message payload.

    Attributes:
        from_email: Sender email address.
        to_email: Primary recipient email address.
        subject: Email subject line.
        html_body: Optional HTML body content.
        text_body: Optional plain text body content.
        cc: Optional list of carbon-copy recipient addresses.
        bcc: Optional list of blind carbon-copy recipient addresses.
        reply_to: Optional reply-to address.
    """

    from_email: str
    to_email: str
    subject: str
    html_body: Optional[str] = None
    text_body: Optional[str] = None
    cc: List[str] = field(default_factory=list)
    bcc: List[str] = field(default_factory=list)
    reply_to: Optional[str] = None


@dataclass
class EmailSendResult:
    """Result of an email dispatch attempt.

    Attributes:
        success: Whether the message was accepted by the transport/provider.
        provider_message_id: Provider-assigned message identifier, if any.
        error_message: Error detail when `success` is False.
    """

    success: bool
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None


class EmailProviderInterface(ABC):
    """Abstract contract for email delivery providers."""

    @abstractmethod
    async def send(self, message: EmailMessage) -> EmailSendResult:
        """Send an email message.

        Args:
            message: Email message to dispatch.

        Returns:
            The outcome of the dispatch attempt.
        """
        raise NotImplementedError


@dataclass
class SMTPConfig:
    """Configuration required to connect to an SMTP relay.

    Attributes:
        host: SMTP server hostname.
        port: SMTP server port.
        username: SMTP authentication username.
        password: SMTP authentication password.
        use_tls: Whether to negotiate STARTTLS on connect.
        timeout_seconds: Socket timeout in seconds.
    """

    host: str
    port: int
    username: str
    password: str
    use_tls: bool = True
    timeout_seconds: int = 30


class SMTPEmailProvider(EmailProviderInterface):
    """SMTP-based email provider implementation.

    Attributes:
        config: SMTP connection configuration.
    """

    def __init__(self, config: SMTPConfig) -> None:
        """Initialize the SMTP provider.

        Args:
            config: SMTP connection configuration.
        """
        self.config = config

    def _build_mime_message(self, message: EmailMessage) -> MIMEMultipart:
        """Construct a MIME message from an `EmailMessage` payload.

        Args:
            message: Email message to convert.

        Returns:
            A fully populated `MIMEMultipart` instance.
        """
        mime_message = MIMEMultipart("alternative")
        mime_message["From"] = message.from_email
        mime_message["To"] = message.to_email
        mime_message["Subject"] = message.subject
        if message.reply_to:
            mime_message["Reply-To"] = message.reply_to
        if message.cc:
            mime_message["Cc"] = ", ".join(message.cc)
        if message.text_body:
            mime_message.attach(MIMEText(message.text_body, "plain"))
        if message.html_body:
            mime_message.attach(MIMEText(message.html_body, "html"))
        return mime_message

    def _send_sync(self, message: EmailMessage) -> EmailSendResult:
        """Send an email synchronously over SMTP.

        Args:
            message: Email message to dispatch.

        Returns:
            The outcome of the dispatch attempt.
        """
        recipients = [message.to_email, *message.cc, *message.bcc]
        mime_message = self._build_mime_message(message)
        try:
            with smtplib.SMTP(
                self.config.host, self.config.port, timeout=self.config.timeout_seconds
            ) as smtp_client:
                if self.config.use_tls:
                    smtp_client.starttls(context=ssl.create_default_context())
                smtp_client.login(self.config.username, self.config.password)
                smtp_client.sendmail(
                    message.from_email, recipients, mime_message.as_string()
                )
            return EmailSendResult(
                success=True, provider_message_id=str(uuid.uuid4())
            )
        except (smtplib.SMTPException, OSError) as exc:
            return EmailSendResult(success=False, error_message=str(exc))

    async def send(self, message: EmailMessage) -> EmailSendResult:
        """Send an email message via SMTP without blocking the event loop.

        Args:
            message: Email message to dispatch.

        Returns:
            The outcome of the dispatch attempt.
        """
        return await anyio.to_thread.run_sync(partial(self._send_sync, message))


class EmailProviderFactory:
    """Factory that resolves the appropriate email provider implementation."""

    @staticmethod
    def create(
        provider: EmailProvider, smtp_config: Optional[SMTPConfig] = None
    ) -> EmailProviderInterface:
        """Instantiate an email provider implementation.

        Args:
            provider: Identifier of the provider to instantiate.
            smtp_config: SMTP configuration, required when `provider` is
                `EmailProvider.SMTP`.

        Returns:
            A concrete `EmailProviderInterface` implementation.

        Raises:
            ValueError: If the requested provider is unsupported or its
                required configuration is missing.
        """
        if provider == EmailProvider.SMTP:
            if smtp_config is None:
                raise ValueError("smtp_config is required for the SMTP provider")
            return SMTPEmailProvider(smtp_config)
        raise ValueError(f"unsupported email provider: {provider}")