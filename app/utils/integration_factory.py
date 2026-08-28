"""
backend/app/utils/integration_factory.py

Reusable factory utilities for the Integration Management module.

These factories translate a persisted :class:`app.models.integration.
Integration` (or an equivalent create/update payload) into a
structured, provider-agnostic :class:`ClientConfig` describing exactly
what a real outbound client/SDK adapter would need to be constructed
with -- base URL, timeout/retry policy, resolved authentication
material, and provider-specific configuration.

This module deliberately stops short of performing any live network
I/O or importing any third-party provider SDK: wiring an actual
provider client (boto3, stripe, openai, etc.) belongs to a
provider-specific adapter that consumes the `ClientConfig` this module
produces. That keeps this factory layer:
    - Free of hard dependencies on optional third-party packages.
    - Fully unit-testable without network access or provider credentials.
    - The single place `integration_type` -> "what kind of client would
      I build" dispatch logic lives, mirroring how
      `app.services.integration_service.IntegrationService` centralizes
      `provider` -> `integration_type` business rules.

Mirrors the project's `app/utils/*` conventions: pure, side-effect-free
helper classes with no session/database access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from app.core.exceptions import BusinessRuleException, ValidationException
from app.models.integration import (
    AuthenticationType,
    Integration,
    IntegrationProvider,
    IntegrationType,
)

__all__ = [
    "ClientConfig",
    "AuthenticationFactory",
    "BaseProviderFactory",
    "StorageProviderFactory",
    "PaymentProviderFactory",
    "AIProviderFactory",
    "MessagingProviderFactory",
    "CalendarProviderFactory",
    "WebhookProviderFactory",
    "RESTClientFactory",
    "IntegrationProviderFactory",
    "provider_factory",
]


# ---------------------------------------------------------------------------
# Shared value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClientConfig:
    """Provider-agnostic description of an outbound client to construct.

    A `ClientConfig` is the hand-off point between this factory layer
    and an (out of scope) provider-specific adapter that would use it
    to instantiate a real SDK/HTTP client.

    Attributes:
        integration_id: Id of the source integration, if persisted.
        integration_type: The integration's functional category.
        provider: The specific third-party provider.
        base_url: Base URL of the external API/service, if applicable.
        api_version: API version identifier, if applicable.
        webhook_url: Associated webhook URL, if applicable.
        timeout_seconds: Per-request timeout, in seconds.
        retry_count: Number of retries to attempt on failure.
        rate_limit_per_minute: Client-side rate limit, if enforced.
        auth: Resolved authentication material, see
            :meth:`AuthenticationFactory.create`.
        configuration: Non-secret, provider-specific settings.
        extra: Free-form bag for provider-family-specific derived
            values (e.g. a computed S3 endpoint, a resolved OAuth
            token URL) that don't warrant a dedicated field.
    """

    integration_type: IntegrationType
    provider: IntegrationProvider
    timeout_seconds: int
    retry_count: int
    auth: dict[str, Any]
    integration_id: Optional[uuid.UUID] = None
    base_url: Optional[str] = None
    api_version: Optional[str] = None
    webhook_url: Optional[str] = None
    rate_limit_per_minute: Optional[int] = None
    configuration: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Authentication Factory
# ---------------------------------------------------------------------------
class AuthenticationFactory:
    """Resolves credentials into a normalized, provider-agnostic auth payload.

    The resulting dict is shaped so a downstream adapter can apply it
    to an outbound request/SDK client without needing to know how the
    integration was originally authenticated -- only its `scheme`.
    """

    @staticmethod
    def create(
        authentication_type: AuthenticationType,
        credentials: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Builds a normalized authentication payload for a given auth type.

        Args:
            authentication_type: The authentication mechanism to resolve.
            credentials: The (decrypted, at the caller's responsibility)
                raw credentials payload backing this authentication type.

        Returns:
            dict[str, Any]: A normalized `{"scheme": ..., ...}` payload
            describing how to authenticate outbound requests.

        Raises:
            BusinessRuleException: If `authentication_type` requires
                credentials that were not supplied, or is missing a
                specific key that mechanism requires.
        """
        credentials = credentials or {}
        builder = AuthenticationFactory._BUILDERS.get(authentication_type)
        if builder is None:
            raise BusinessRuleException(
                f"No authentication builder registered for "
                f"'{authentication_type.value}'."
            )
        return builder(credentials)

    @staticmethod
    def _build_api_key(credentials: dict[str, Any]) -> dict[str, Any]:
        """Builds an API-key auth payload.

        Args:
            credentials: Must contain an `api_key`.

        Returns:
            dict[str, Any]: The normalized auth payload.

        Raises:
            BusinessRuleException: If `api_key` is missing.
        """
        api_key = credentials.get("api_key")
        if not api_key:
            raise BusinessRuleException(
                "credentials.api_key is required for API_KEY authentication."
            )
        header_name = credentials.get("header_name", "X-API-Key")
        return {"scheme": "api_key", "header_name": header_name, "api_key": api_key}

    @staticmethod
    def _build_oauth2(credentials: dict[str, Any]) -> dict[str, Any]:
        """Builds an OAuth2 auth payload.

        Args:
            credentials: Must contain `client_id`/`client_secret`, plus
                either an `access_token` or `refresh_token`.

        Returns:
            dict[str, Any]: The normalized auth payload.

        Raises:
            BusinessRuleException: If required OAuth2 fields are missing.
        """
        required = ("client_id", "client_secret")
        missing = [key for key in required if not credentials.get(key)]
        if missing:
            raise BusinessRuleException(
                f"credentials missing required OAuth2 field(s): "
                f"{', '.join(missing)}."
            )
        if not credentials.get("access_token") and not credentials.get(
            "refresh_token"
        ):
            raise BusinessRuleException(
                "credentials must include an access_token or refresh_token "
                "for OAuth2 authentication."
            )
        return {
            "scheme": "oauth2",
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "access_token": credentials.get("access_token"),
            "refresh_token": credentials.get("refresh_token"),
            "token_url": credentials.get("token_url"),
        }

    @staticmethod
    def _build_basic_auth(credentials: dict[str, Any]) -> dict[str, Any]:
        """Builds a Basic-auth payload.

        Args:
            credentials: Must contain `username`/`password`.

        Returns:
            dict[str, Any]: The normalized auth payload.

        Raises:
            BusinessRuleException: If `username`/`password` are missing.
        """
        missing = [k for k in ("username", "password") if not credentials.get(k)]
        if missing:
            raise BusinessRuleException(
                f"credentials missing required Basic-auth field(s): "
                f"{', '.join(missing)}."
            )
        return {
            "scheme": "basic_auth",
            "username": credentials["username"],
            "password": credentials["password"],
        }

    @staticmethod
    def _build_bearer_token(credentials: dict[str, Any]) -> dict[str, Any]:
        """Builds a bearer-token auth payload.

        Args:
            credentials: Must contain a `token`.

        Returns:
            dict[str, Any]: The normalized auth payload.

        Raises:
            BusinessRuleException: If `token` is missing.
        """
        token = credentials.get("token")
        if not token:
            raise BusinessRuleException(
                "credentials.token is required for BEARER_TOKEN authentication."
            )
        return {"scheme": "bearer_token", "token": token}

    @staticmethod
    def _build_hmac_signature(credentials: dict[str, Any]) -> dict[str, Any]:
        """Builds an HMAC-signature auth payload.

        Args:
            credentials: Must contain a `signing_secret`.

        Returns:
            dict[str, Any]: The normalized auth payload.

        Raises:
            BusinessRuleException: If `signing_secret` is missing.
        """
        secret = credentials.get("signing_secret")
        if not secret:
            raise BusinessRuleException(
                "credentials.signing_secret is required for HMAC_SIGNATURE "
                "authentication."
            )
        return {
            "scheme": "hmac_signature",
            "signing_secret": secret,
            "algorithm": credentials.get("algorithm", "sha256"),
        }

    @staticmethod
    def _build_none(credentials: dict[str, Any]) -> dict[str, Any]:
        """Builds an empty auth payload for unauthenticated integrations.

        Args:
            credentials: Ignored.

        Returns:
            dict[str, Any]: `{"scheme": "none"}`.
        """
        return {"scheme": "none"}

    _BUILDERS: dict[AuthenticationType, Callable[[dict[str, Any]], dict[str, Any]]] = {
        AuthenticationType.API_KEY: _build_api_key.__func__,
        AuthenticationType.OAUTH2: _build_oauth2.__func__,
        AuthenticationType.BASIC_AUTH: _build_basic_auth.__func__,
        AuthenticationType.BEARER_TOKEN: _build_bearer_token.__func__,
        AuthenticationType.HMAC_SIGNATURE: _build_hmac_signature.__func__,
        AuthenticationType.NONE: _build_none.__func__,
    }


# ---------------------------------------------------------------------------
# Base provider factory
# ---------------------------------------------------------------------------
class SupportsIntegrationLike(Protocol):
    """Structural type for anything shaped enough to build a `ClientConfig`."""

    provider: IntegrationProvider
    integration_type: IntegrationType
    authentication_type: AuthenticationType
    configuration: Optional[dict[str, Any]]
    credentials: Optional[dict[str, Any]]
    base_url: Optional[str]
    api_version: Optional[str]
    webhook_url: Optional[str]
    timeout_seconds: int
    retry_count: int
    rate_limit_per_minute: Optional[int]


class BaseProviderFactory:
    """Base class for functional-category-specific provider factories.

    Attributes:
        supported_types: The `IntegrationType` values this factory
            knows how to build a `ClientConfig` for.
    """

    supported_types: frozenset[IntegrationType] = frozenset()

    def supports(self, integration_type: IntegrationType) -> bool:
        """Reports whether this factory can build a config for `integration_type`.

        Args:
            integration_type: The integration type to check.

        Returns:
            bool: `True` if this factory supports `integration_type`.
        """
        return integration_type in self.supported_types

    def build(self, integration: SupportsIntegrationLike) -> ClientConfig:
        """Builds a `ClientConfig` for the given integration-like object.

        Args:
            integration: An `Integration` ORM instance (or any object
                exposing the same attributes).

        Returns:
            ClientConfig: The resolved, provider-agnostic client config.

        Raises:
            BusinessRuleException: If `integration.integration_type` is
                not supported by this factory.
        """
        if not self.supports(integration.integration_type):
            raise BusinessRuleException(
                f"{type(self).__name__} does not support integration_type "
                f"'{integration.integration_type.value}'."
            )
        auth = AuthenticationFactory.create(
            integration.authentication_type, integration.credentials
        )
        config = ClientConfig(
            integration_id=getattr(integration, "id", None),
            integration_type=integration.integration_type,
            provider=integration.provider,
            base_url=integration.base_url,
            api_version=integration.api_version,
            webhook_url=integration.webhook_url,
            timeout_seconds=integration.timeout_seconds,
            retry_count=integration.retry_count,
            rate_limit_per_minute=integration.rate_limit_per_minute,
            auth=auth,
            configuration=dict(integration.configuration or {}),
        )
        return self._enrich(integration, config)

    def _enrich(
        self, integration: SupportsIntegrationLike, config: ClientConfig
    ) -> ClientConfig:
        """Hook for subclasses to add provider-family-specific derived values.

        Args:
            integration: The source integration-like object.
            config: The base `ClientConfig` already populated with
                common fields.

        Returns:
            ClientConfig: `config`, optionally with `config.extra`
            populated by a subclass override.
        """
        return config


# ---------------------------------------------------------------------------
# Concrete, functional-category provider factories
# ---------------------------------------------------------------------------
class StorageProviderFactory(BaseProviderFactory):
    """Builds `ClientConfig`s for object/blob storage providers."""

    supported_types = frozenset({IntegrationType.STORAGE})

    def _enrich(
        self, integration: SupportsIntegrationLike, config: ClientConfig
    ) -> ClientConfig:
        """Derives storage-family-specific extras (bucket/container/root)."""
        cfg = config.configuration
        if integration.provider == IntegrationProvider.AWS_S3:
            config.extra["bucket_name"] = cfg.get("bucket_name")
            config.extra["region"] = cfg.get("region")
        elif integration.provider == IntegrationProvider.AZURE_BLOB_STORAGE:
            config.extra["container_name"] = cfg.get("container_name")
            config.extra["account_name"] = cfg.get("account_name")
        elif integration.provider == IntegrationProvider.GOOGLE_DRIVE:
            config.extra["root_folder_id"] = cfg.get("root_folder_id")
        return config


class PaymentProviderFactory(BaseProviderFactory):
    """Builds `ClientConfig`s for payment-gateway providers."""

    supported_types = frozenset({IntegrationType.PAYMENT_GATEWAY})

    def _enrich(
        self, integration: SupportsIntegrationLike, config: ClientConfig
    ) -> ClientConfig:
        """Derives payment-family-specific extras (webhook signing header)."""
        cfg = config.configuration
        config.extra["webhook_signing_header"] = cfg.get(
            "webhook_signing_header", "X-Signature"
        )
        return config


class AIProviderFactory(BaseProviderFactory):
    """Builds `ClientConfig`s for LLM/AI-inference providers."""

    supported_types = frozenset({IntegrationType.AI_PROVIDER})

    def _enrich(
        self, integration: SupportsIntegrationLike, config: ClientConfig
    ) -> ClientConfig:
        """Derives AI-family-specific extras (model, default sampling params)."""
        cfg = config.configuration
        config.extra["model"] = cfg.get("model")
        config.extra["max_tokens"] = cfg.get("max_tokens", 1024)
        config.extra["temperature"] = cfg.get("temperature", 0.7)
        return config


class MessagingProviderFactory(BaseProviderFactory):
    """Builds `ClientConfig`s for email/SMS/WhatsApp/push messaging providers."""

    supported_types = frozenset(
        {
            IntegrationType.EMAIL,
            IntegrationType.SMS,
            IntegrationType.WHATSAPP,
            IntegrationType.NOTIFICATION,
        }
    )

    def _enrich(
        self, integration: SupportsIntegrationLike, config: ClientConfig
    ) -> ClientConfig:
        """Derives messaging-family-specific extras per provider."""
        cfg = config.configuration
        if integration.provider == IntegrationProvider.SMTP:
            config.extra["host"] = cfg.get("host")
            config.extra["port"] = cfg.get("port")
            config.extra["use_tls"] = cfg.get("use_tls", True)
        elif integration.provider == IntegrationProvider.SMS_PROVIDER:
            config.extra["sender_id"] = cfg.get("sender_id")
        elif integration.provider == IntegrationProvider.WHATSAPP_BUSINESS:
            config.extra["phone_number_id"] = cfg.get("phone_number_id")
        elif integration.provider == IntegrationProvider.FIREBASE:
            config.extra["project_id"] = cfg.get("project_id")
        return config


class CalendarProviderFactory(BaseProviderFactory):
    """Builds `ClientConfig`s for calendar-sync providers."""

    supported_types = frozenset({IntegrationType.CALENDAR})

    def _enrich(
        self, integration: SupportsIntegrationLike, config: ClientConfig
    ) -> ClientConfig:
        """Derives calendar-family-specific extras (calendar id, timezone)."""
        cfg = config.configuration
        config.extra["calendar_id"] = cfg.get("calendar_id")
        config.extra["timezone"] = cfg.get("timezone", "UTC")
        return config


class WebhookProviderFactory(BaseProviderFactory):
    """Builds `ClientConfig`s for outbound webhook targets."""

    supported_types = frozenset({IntegrationType.WEBHOOK})

    def _enrich(
        self, integration: SupportsIntegrationLike, config: ClientConfig
    ) -> ClientConfig:
        """Derives webhook-family-specific extras (delivery target, signing)."""
        if not config.webhook_url:
            raise BusinessRuleException(
                "webhook_url is required to build a WEBHOOK client config."
            )
        cfg = config.configuration
        config.extra["signing_header"] = cfg.get("signing_header", "X-Signature")
        return config


class RESTClientFactory(BaseProviderFactory):
    """Builds `ClientConfig`s for generic, custom REST API integrations."""

    supported_types = frozenset({IntegrationType.CUSTOM_API})

    def _enrich(
        self, integration: SupportsIntegrationLike, config: ClientConfig
    ) -> ClientConfig:
        """Derives generic-REST-family extras (default headers)."""
        if not config.base_url:
            raise BusinessRuleException(
                "base_url is required to build a CUSTOM_API client config."
            )
        cfg = config.configuration
        config.extra["default_headers"] = cfg.get("default_headers", {})
        return config


# ---------------------------------------------------------------------------
# Top-level provider factory (dispatch / registry)
# ---------------------------------------------------------------------------
class IntegrationProviderFactory:
    """Top-level factory that dispatches to the correct functional-category factory.

    Acts as an extensible registry: additional `IntegrationType`s can
    be wired to a custom `BaseProviderFactory` subclass via
    :meth:`register` without modifying this class.

    Attributes:
        _registry: Mapping of `IntegrationType` to the `BaseProviderFactory`
            instance responsible for building its `ClientConfig`.
    """

    def __init__(self) -> None:
        """Initializes the registry with the built-in factory set."""
        self._registry: dict[IntegrationType, BaseProviderFactory] = {}
        for factory in (
            MessagingProviderFactory(),
            CalendarProviderFactory(),
            StorageProviderFactory(),
            AIProviderFactory(),
            PaymentProviderFactory(),
            WebhookProviderFactory(),
            RESTClientFactory(),
        ):
            for integration_type in factory.supported_types:
                self._registry[integration_type] = factory

    def register(
        self, integration_type: IntegrationType, factory: BaseProviderFactory
    ) -> None:
        """Registers (or overrides) the factory responsible for a given type.

        Args:
            integration_type: The `IntegrationType` to register.
            factory: The `BaseProviderFactory` instance to associate
                with `integration_type`.
        """
        self._registry[integration_type] = factory

    def get_factory(self, integration_type: IntegrationType) -> BaseProviderFactory:
        """Looks up the factory responsible for a given integration type.

        Args:
            integration_type: The integration type to look up.

        Returns:
            BaseProviderFactory: The responsible factory instance.

        Raises:
            BusinessRuleException: If no factory is registered for
                `integration_type`.
        """
        factory = self._registry.get(integration_type)
        if factory is None:
            raise BusinessRuleException(
                f"No provider factory registered for integration_type "
                f"'{integration_type.value}'."
            )
        return factory

    def create_client_config(
        self, integration: SupportsIntegrationLike
    ) -> ClientConfig:
        """Builds a `ClientConfig` for the given integration, dispatching by type.

        Args:
            integration: An `Integration` ORM instance (or any object
                exposing the same attributes).

        Returns:
            ClientConfig: The resolved, provider-agnostic client config.

        Raises:
            BusinessRuleException: If no factory is registered for
                `integration.integration_type`, or the resolved
                factory's build-time invariants are violated.
        """
        factory = self.get_factory(integration.integration_type)
        return factory.build(integration)


#: Module-level singleton, mirroring the stateless/reusable nature of
#: the other `app/utils/*` helpers in this project.
provider_factory = IntegrationProviderFactory()