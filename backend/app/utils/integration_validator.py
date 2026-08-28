"""
backend/app/utils/integration_validator.py

Reusable, side-effect-free validation utilities for the Integration
Management module.

`IntegrationValidator` collects the same category of business-rule
checks performed inline by
`app.services.integration_service.IntegrationService`, exposed as a
standalone, dependency-free utility so they can also be reused by
`app.utils.integration_factory` (or any future caller, e.g. a CLI
provisioning script or a background reconciliation job) without going
through the service/repository layer.

This module never raises `HTTPException` -- it raises this project's
centralized `app.core.exceptions` `AppException` subclasses
(`ValidationException`, `BusinessRuleException`), exactly like
`IntegrationService`, so callers get identical error semantics
regardless of which layer performed the check.

Mirrors: `app.services.integration_service` validation conventions
(static methods, one rule per method, module-level lookup tables).
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from app.core.exceptions import BusinessRuleException, ValidationException
from app.models.integration import (
    AuthenticationType,
    IntegrationProvider,
    IntegrationType,
)
from app.schemas.integration import (
    MAX_RETRY_COUNT,
    MAX_TIMEOUT_SECONDS,
    MIN_RETRY_COUNT,
    MIN_TIMEOUT_SECONDS,
)

__all__ = ["IntegrationValidator"]

#: A given `IntegrationProvider` is only ever valid for a subset of
#: `IntegrationType` values. Duplicated locally (mirroring the
#: equivalent maps in `app.schemas.integration` and
#: `app.services.integration_service`) so this utility module carries
#: no dependency on either layer's private symbols and can be reused
#: standalone.
_PROVIDER_TYPE_MAP: dict[IntegrationProvider, IntegrationType] = {
    IntegrationProvider.SMTP: IntegrationType.EMAIL,
    IntegrationProvider.SMS_PROVIDER: IntegrationType.SMS,
    IntegrationProvider.WHATSAPP_BUSINESS: IntegrationType.WHATSAPP,
    IntegrationProvider.GOOGLE_CALENDAR: IntegrationType.CALENDAR,
    IntegrationProvider.GOOGLE_DRIVE: IntegrationType.STORAGE,
    IntegrationProvider.AWS_S3: IntegrationType.STORAGE,
    IntegrationProvider.AZURE_BLOB_STORAGE: IntegrationType.STORAGE,
    IntegrationProvider.FIREBASE: IntegrationType.NOTIFICATION,
    IntegrationProvider.OPENAI: IntegrationType.AI_PROVIDER,
    IntegrationProvider.ANTHROPIC: IntegrationType.AI_PROVIDER,
    IntegrationProvider.GEMINI: IntegrationType.AI_PROVIDER,
    IntegrationProvider.HUGGING_FACE: IntegrationType.AI_PROVIDER,
    IntegrationProvider.RAZORPAY: IntegrationType.PAYMENT_GATEWAY,
    IntegrationProvider.STRIPE: IntegrationType.PAYMENT_GATEWAY,
    IntegrationProvider.WEBHOOK_TARGET: IntegrationType.WEBHOOK,
    IntegrationProvider.CUSTOM_REST_API: IntegrationType.CUSTOM_API,
}

#: Provider-specific required `configuration` keys. Mirrors
#: `app.services.integration_service._REQUIRED_CONFIG_KEYS`.
_REQUIRED_CONFIG_KEYS: dict[IntegrationProvider, tuple[str, ...]] = {
    IntegrationProvider.SMTP: ("host", "port"),
    IntegrationProvider.SMS_PROVIDER: ("sender_id",),
    IntegrationProvider.WHATSAPP_BUSINESS: ("phone_number_id",),
    IntegrationProvider.GOOGLE_CALENDAR: ("calendar_id",),
    IntegrationProvider.GOOGLE_DRIVE: ("root_folder_id",),
    IntegrationProvider.AWS_S3: ("bucket_name", "region"),
    IntegrationProvider.AZURE_BLOB_STORAGE: ("container_name", "account_name"),
    IntegrationProvider.FIREBASE: ("project_id",),
    IntegrationProvider.OPENAI: ("model",),
    IntegrationProvider.ANTHROPIC: ("model",),
    IntegrationProvider.GEMINI: ("model",),
    IntegrationProvider.HUGGING_FACE: ("model",),
    IntegrationProvider.RAZORPAY: (),
    IntegrationProvider.STRIPE: (),
    IntegrationProvider.WEBHOOK_TARGET: (),
    IntegrationProvider.CUSTOM_REST_API: (),
}

#: Integration types that must carry a `base_url`.
_BASE_URL_REQUIRED_TYPES: frozenset[IntegrationType] = frozenset(
    {IntegrationType.CUSTOM_API}
)

#: Integration types that must carry a `webhook_url`.
_WEBHOOK_URL_REQUIRED_TYPES: frozenset[IntegrationType] = frozenset(
    {IntegrationType.WEBHOOK}
)

#: Minimum acceptable length for a raw API key, a conservative floor
#: to reject obviously-placeholder values (e.g. `"x"`, `"test"`).
_MIN_API_KEY_LENGTH: int = 8

#: OAuth2 fields that must always be present in `credentials`.
_OAUTH2_REQUIRED_CREDENTIAL_FIELDS: tuple[str, ...] = ("client_id", "client_secret")

#: One of these OAuth2 grant-material fields must also be present.
_OAUTH2_GRANT_FIELDS: tuple[str, ...] = (
    "access_token",
    "refresh_token",
    "authorization_code",
)

_URL_SCHEME_PATTERN = re.compile(r"^https?://", re.IGNORECASE)


class IntegrationValidator:
    """Stateless collection of reusable integration business-rule validators.

    Every method is a `staticmethod` that either returns `None` (valid)
    or raises a `ValidationException`/`BusinessRuleException`. Methods
    are intentionally narrow -- one rule per method -- so callers can
    compose exactly the checks relevant to their context (a full
    create, a partial update, or a factory build-time preflight).
    """

    # ------------------------------------------------------------------
    # Provider / type
    # ------------------------------------------------------------------
    @staticmethod
    def validate_provider(provider: IntegrationProvider) -> None:
        """Validates that `provider` is a supported `IntegrationProvider`.

        Args:
            provider: The provider to validate.

        Raises:
            ValidationException: If `provider` is not a member of
                `IntegrationProvider`.
        """
        if not isinstance(provider, IntegrationProvider):
            raise ValidationException(
                f"'{provider}' is not a supported integration provider."
            )

    @staticmethod
    def validate_provider_type_pairing(
        provider: IntegrationProvider, integration_type: IntegrationType
    ) -> None:
        """Validates that `provider` is a recognized pairing for `integration_type`.

        Args:
            provider: The integration's provider.
            integration_type: The integration's functional category.

        Raises:
            BusinessRuleException: If `provider` is not valid for
                `integration_type` per the supported provider/type map.
        """
        expected_type = _PROVIDER_TYPE_MAP.get(provider)
        if expected_type is not None and expected_type != integration_type:
            raise BusinessRuleException(
                f"provider '{provider.value}' is not valid for "
                f"integration_type '{integration_type.value}'; expected "
                f"'{expected_type.value}'."
            )

    # ------------------------------------------------------------------
    # Credentials / authentication
    # ------------------------------------------------------------------
    @staticmethod
    def validate_credentials(
        authentication_type: AuthenticationType,
        credentials: Optional[dict[str, Any]],
    ) -> None:
        """Validates that credentials are present when `authentication_type` requires them.

        Args:
            authentication_type: The authentication mechanism to validate.
            credentials: The raw credentials payload, if any.

        Raises:
            ValidationException: If `credentials` was supplied but is
                not a JSON object.
            BusinessRuleException: If `authentication_type` is not
                `NONE` but no credentials were supplied.
        """
        if credentials is not None and not isinstance(credentials, dict):
            raise ValidationException("credentials must be a JSON object.")
        if authentication_type != AuthenticationType.NONE and not credentials:
            raise BusinessRuleException(
                f"credentials are required for authentication_type "
                f"'{authentication_type.value}'."
            )

    @staticmethod
    def validate_api_keys(credentials: Optional[dict[str, Any]]) -> None:
        """Validates an API-key credentials payload.

        Args:
            credentials: Must contain a non-blank `api_key` field of
                at least `_MIN_API_KEY_LENGTH` characters.

        Raises:
            BusinessRuleException: If `api_key` is missing, blank, or
                too short to plausibly be a real key.
        """
        credentials = credentials or {}
        api_key = credentials.get("api_key")
        if not api_key or not isinstance(api_key, str) or not api_key.strip():
            raise BusinessRuleException(
                "credentials.api_key is required for API_KEY authentication."
            )
        if len(api_key.strip()) < _MIN_API_KEY_LENGTH:
            raise BusinessRuleException(
                f"credentials.api_key must be at least "
                f"{_MIN_API_KEY_LENGTH} characters."
            )

    @staticmethod
    def validate_oauth_configuration(
        credentials: Optional[dict[str, Any]],
        configuration: Optional[dict[str, Any]] = None,
    ) -> None:
        """Validates an OAuth2 credentials/configuration payload.

        Args:
            credentials: Must contain `client_id`/`client_secret`, plus
                at least one of `access_token`, `refresh_token`, or
                `authorization_code`.
            configuration: If it declares a `token_url` or
                `authorize_url`, both must be well-formed http(s) URLs.

        Raises:
            BusinessRuleException: If required OAuth2 credential
                fields are missing.
            ValidationException: If a declared `token_url`/
                `authorize_url` is not a well-formed http(s) URL.
        """
        credentials = credentials or {}
        missing = [
            key
            for key in _OAUTH2_REQUIRED_CREDENTIAL_FIELDS
            if not credentials.get(key)
        ]
        if missing:
            raise BusinessRuleException(
                f"credentials missing required OAuth2 field(s): "
                f"{', '.join(missing)}."
            )
        if not any(credentials.get(field) for field in _OAUTH2_GRANT_FIELDS):
            raise BusinessRuleException(
                "credentials must include one of: "
                f"{', '.join(_OAUTH2_GRANT_FIELDS)}."
            )

        configuration = configuration or {}
        for url_field in ("token_url", "authorize_url"):
            url_value = configuration.get(url_field)
            if url_value:
                IntegrationValidator._assert_http_url(url_value, field_name=url_field)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @staticmethod
    def validate_configuration(
        provider: IntegrationProvider, configuration: Optional[dict[str, Any]]
    ) -> None:
        """Validates provider-specific `configuration` payload completeness.

        Args:
            provider: The integration's provider, used to look up
                required configuration keys.
            configuration: The non-secret, provider-specific settings
                payload to validate.

        Raises:
            ValidationException: If `configuration` was supplied but
                is not a JSON object, or is missing a key required for
                `provider`.
        """
        if configuration is not None and not isinstance(configuration, dict):
            raise ValidationException("configuration must be a JSON object.")

        required_keys = _REQUIRED_CONFIG_KEYS.get(provider, ())
        if not required_keys:
            return

        payload = configuration or {}
        missing = [key for key in required_keys if not payload.get(key)]
        if missing:
            raise ValidationException(
                f"configuration for provider '{provider.value}' is missing "
                f"required key(s): {', '.join(missing)}."
            )

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------
    @staticmethod
    def _assert_http_url(value: str, *, field_name: str) -> None:
        """Asserts that `value` is a well-formed http(s) URL.

        Args:
            value: The URL string to validate.
            field_name: Name of the field being validated, used in the
                raised error message.

        Raises:
            ValidationException: If `value` does not use the
                `http://`/`https://` scheme or lacks a network location.
        """
        if not _URL_SCHEME_PATTERN.match(value):
            raise ValidationException(
                f"{field_name} must start with 'http://' or 'https://'."
            )
        parsed = urlparse(value)
        if not parsed.netloc:
            raise ValidationException(f"{field_name} is not a well-formed URL.")

    @staticmethod
    def validate_base_url(
        base_url: Optional[str], integration_type: IntegrationType
    ) -> None:
        """Validates `base_url` presence/shape against `integration_type`.

        Args:
            base_url: The base URL to validate, if supplied.
            integration_type: The integration's functional category.

        Raises:
            BusinessRuleException: If `integration_type` requires a
                base URL and none was supplied.
            ValidationException: If a supplied `base_url` is not a
                well-formed http(s) URL.
        """
        if integration_type in _BASE_URL_REQUIRED_TYPES and not base_url:
            raise BusinessRuleException(
                f"base_url is required for integration_type "
                f"'{integration_type.value}'."
            )
        if base_url:
            IntegrationValidator._assert_http_url(base_url, field_name="base_url")

    @staticmethod
    def validate_webhook_url(
        webhook_url: Optional[str], integration_type: IntegrationType
    ) -> None:
        """Validates `webhook_url` presence/shape against `integration_type`.

        Args:
            webhook_url: The webhook URL to validate, if supplied.
            integration_type: The integration's functional category.

        Raises:
            BusinessRuleException: If `integration_type` requires a
                webhook URL and none was supplied.
            ValidationException: If a supplied `webhook_url` is not a
                well-formed http(s) URL.
        """
        if integration_type in _WEBHOOK_URL_REQUIRED_TYPES and not webhook_url:
            raise BusinessRuleException(
                f"webhook_url is required for integration_type "
                f"'{integration_type.value}'."
            )
        if webhook_url:
            IntegrationValidator._assert_http_url(
                webhook_url, field_name="webhook_url"
            )

    # ------------------------------------------------------------------
    # Operational parameters
    # ------------------------------------------------------------------
    @staticmethod
    def validate_timeout(timeout_seconds: int) -> None:
        """Validates `timeout_seconds` falls within the supported bounds.

        Args:
            timeout_seconds: The per-request timeout, in seconds.

        Raises:
            ValidationException: If `timeout_seconds` is outside
                `[MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS]`.
        """
        if not (MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS):
            raise ValidationException(
                f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} and "
                f"{MAX_TIMEOUT_SECONDS}."
            )

    @staticmethod
    def validate_retry_count(retry_count: int) -> None:
        """Validates `retry_count` falls within the supported bounds.

        Args:
            retry_count: The number of retries to attempt on failure.

        Raises:
            ValidationException: If `retry_count` is outside
                `[MIN_RETRY_COUNT, MAX_RETRY_COUNT]`.
        """
        if not (MIN_RETRY_COUNT <= retry_count <= MAX_RETRY_COUNT):
            raise ValidationException(
                f"retry_count must be between {MIN_RETRY_COUNT} and "
                f"{MAX_RETRY_COUNT}."
            )

    # ------------------------------------------------------------------
    # Composite entry point
    # ------------------------------------------------------------------
    @staticmethod
    def validate_all(
        *,
        provider: IntegrationProvider,
        integration_type: IntegrationType,
        authentication_type: AuthenticationType,
        configuration: Optional[dict[str, Any]],
        credentials: Optional[dict[str, Any]],
        base_url: Optional[str],
        webhook_url: Optional[str],
        timeout_seconds: int,
        retry_count: int,
    ) -> None:
        """Runs every applicable validator for a full integration payload.

        Composes the individual validators above in a fixed order, plus
        the authentication-specific deep checks (`validate_api_keys`,
        `validate_oauth_configuration`) when `authentication_type`
        indicates they apply.

        Args:
            provider: The integration's provider.
            integration_type: The integration's functional category.
            authentication_type: The authentication mechanism.
            configuration: Non-secret, provider-specific settings.
            credentials: Raw credentials payload, if any.
            base_url: Base URL of the external service, if any.
            webhook_url: Associated webhook URL, if any.
            timeout_seconds: Per-request timeout, in seconds.
            retry_count: Number of retries on a failed request.

        Raises:
            ValidationException: See individual validators.
            BusinessRuleException: See individual validators.
        """
        IntegrationValidator.validate_provider(provider)
        IntegrationValidator.validate_provider_type_pairing(provider, integration_type)
        IntegrationValidator.validate_credentials(authentication_type, credentials)

        if authentication_type == AuthenticationType.API_KEY:
            IntegrationValidator.validate_api_keys(credentials)
        elif authentication_type == AuthenticationType.OAUTH2:
            IntegrationValidator.validate_oauth_configuration(
                credentials, configuration
            )

        IntegrationValidator.validate_configuration(provider, configuration)
        IntegrationValidator.validate_base_url(base_url, integration_type)
        IntegrationValidator.validate_webhook_url(webhook_url, integration_type)
        IntegrationValidator.validate_timeout(timeout_seconds)
        IntegrationValidator.validate_retry_count(retry_count)