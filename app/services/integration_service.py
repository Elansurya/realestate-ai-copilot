"""
backend/app/services/integration_service.py

Service (business-logic) layer for the Integration Management module
of the Enterprise Real Estate AI Copilot CRM.

`IntegrationService` is the single orchestration point between the API
layer (`app.api.v1.integration`, out of scope for this file) and
persistence (`app.repositories.integration_repository
.IntegrationRepository`). It is responsible for:

    * Validating provider/type/authentication/configuration/URL/
      timeout/retry business rules *before* any repository call that
      would otherwise persist an illegal state, beyond what the
      Pydantic schemas (`app.schemas.integration`) already enforce at
      the request-shape level -- this module re-checks the rules that
      matter across partial updates and state the schemas alone cannot
      see (e.g. an existing row's persisted `credentials`).
    * Enabling/disabling integrations, recording health-check outcomes
      and sync timestamps, and computing aggregate statistics.
    * Performing a structural "connection test" ahead of a real health
      check. Actually placing a live outbound call to a given
      provider (SMTP, S3, Stripe, etc.) requires a provider-specific
      HTTP/SDK client, which lives in this project's (out of scope,
      not part of the seven referenced files) `app.utils` adapters.
      This service intentionally stops at readiness validation --
      confirming the integration *has* what it would need to attempt
      a live call -- and raises `ExternalServiceException` only when
      wiring in an actual adapter call, so the boundary between
      "this integration is well-formed" and "this integration is
      currently reachable" stays explicit.
    * Raising ONLY this project's centralized `app.core.exceptions`
      `AppException` subclasses (`ValidationException`,
      `NotFoundException`, `ConflictException`,
      `DuplicateResourceException`, `BusinessRuleException`,
      `ExternalServiceException`). This service never raises
      `HTTPException` -- that translation belongs to the (out of
      scope) router layer via the project's centralized exception
      handlers (`app.core.exceptions.register_exception_handlers`).
    * NOT committing the session. Every mutating method flushes (via
      the repository) but leaves the commit/rollback boundary to the
      caller (the API router wraps each request in a single
      commit-on-success transaction), mirroring
      `app.services.task_service.TaskService`.

Mirrors: app/services/task_service.py and app/services/search_service.py
(naming/style/transaction/exception conventions).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.exceptions import (
    BusinessRuleException,
    ConflictException,
    DuplicateResourceException,
    ExternalServiceException,
    NotFoundException,
    ValidationException,
)
from app.models.integration import (
    AuthenticationType,
    Integration,
    IntegrationProvider,
    IntegrationStatus,
    IntegrationType,
)
from app.repositories.integration_repository import IntegrationRepository
from app.schemas.integration import (
    MAX_RETRY_COUNT,
    MAX_TIMEOUT_SECONDS,
    MIN_RETRY_COUNT,
    MIN_TIMEOUT_SECONDS,
    IntegrationCreate,
    IntegrationFilter,
    IntegrationHealthCheck,
    IntegrationListResponse,
    IntegrationPaginationParams,
    IntegrationResponse,
    IntegrationSortingParams,
    IntegrationStatisticsResponse,
    IntegrationStatusUpdate,
    IntegrationUpdate,
)

__all__ = ["IntegrationService"]

#: A given `IntegrationProvider` is only ever valid for a subset of
#: `IntegrationType` values. Mirrors the equivalent map in
#: `app.schemas.integration` (kept private there); duplicated here so
#: this service does not depend on a schema-module-private symbol and
#: can re-validate this rule independently of the request-shape layer
#: (e.g. against a to-be-persisted dict built from multiple sources).
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

#: Conservative, adjustable set of `configuration` keys expected for
#: providers whose behavior meaningfully depends on them. Providers
#: not listed (or listed with an empty tuple) have no additionally
#: required configuration keys beyond what `credentials` supplies.
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

#: Integration types that must carry a `base_url`, since a REST/API
#: driven integration is meaningless without a target host.
_BASE_URL_REQUIRED_TYPES: frozenset[IntegrationType] = frozenset(
    {IntegrationType.CUSTOM_API}
)

#: Integration types that must carry a `webhook_url`, since the whole
#: point of a `WEBHOOK` integration is the delivery target.
_WEBHOOK_URL_REQUIRED_TYPES: frozenset[IntegrationType] = frozenset(
    {IntegrationType.WEBHOOK}
)


class IntegrationService:
    """Business-logic layer for managing external service integrations.

    Attributes:
        repository: The repository used for all data access.
    """

    def __init__(self, repository: IntegrationRepository) -> None:
        """Initializes the service with its backing repository.

        Args:
            repository: The `IntegrationRepository` instance to
                delegate all persistence and query concerns to.
        """
        self.repository = repository

    # ------------------------------------------------------------------
    # Business-rule validation
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
    def validate_integration_type(integration_type: IntegrationType) -> None:
        """Validates that `integration_type` is a supported `IntegrationType`.

        Args:
            integration_type: The integration type to validate.

        Raises:
            ValidationException: If `integration_type` is not a member
                of `IntegrationType`.
        """
        if not isinstance(integration_type, IntegrationType):
            raise ValidationException(
                f"'{integration_type}' is not a supported integration type."
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
                `integration_type` per the project's supported
                provider/type mapping.
        """
        expected_type = _PROVIDER_TYPE_MAP.get(provider)
        if expected_type is not None and expected_type != integration_type:
            raise BusinessRuleException(
                f"provider '{provider.value}' is not valid for "
                f"integration_type '{integration_type.value}'; expected "
                f"'{expected_type.value}'."
            )

    @staticmethod
    def validate_authentication_type(
        authentication_type: AuthenticationType,
        credentials: Optional[dict[str, Any]],
    ) -> None:
        """Validates the authentication type and its credential requirement.

        Args:
            authentication_type: The authentication mechanism to validate.
            credentials: The (raw, pre-encryption) credentials payload
                that would accompany this authentication type, if any.

        Raises:
            ValidationException: If `authentication_type` is not a
                member of `AuthenticationType`.
            BusinessRuleException: If `authentication_type` is not
                `NONE` but no credentials were supplied.
        """
        if not isinstance(authentication_type, AuthenticationType):
            raise ValidationException(
                f"'{authentication_type}' is not a supported authentication type."
            )
        if authentication_type != AuthenticationType.NONE and not credentials:
            raise BusinessRuleException(
                f"credentials are required for authentication_type "
                f"'{authentication_type.value}'."
            )

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
            ValidationException: If a supplied `webhook_url` does not
                use the `http://`/`https://` scheme.
        """
        if integration_type in _WEBHOOK_URL_REQUIRED_TYPES and not webhook_url:
            raise BusinessRuleException(
                f"webhook_url is required for integration_type "
                f"'{integration_type.value}'."
            )
        if webhook_url and not (
            webhook_url.startswith("http://") or webhook_url.startswith("https://")
        ):
            raise ValidationException(
                "webhook_url must start with 'http://' or 'https://'."
            )

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
            ValidationException: If a supplied `base_url` does not use
                the `http://`/`https://` scheme.
        """
        if integration_type in _BASE_URL_REQUIRED_TYPES and not base_url:
            raise BusinessRuleException(
                f"base_url is required for integration_type "
                f"'{integration_type.value}'."
            )
        if base_url and not (
            base_url.startswith("http://") or base_url.startswith("https://")
        ):
            raise ValidationException(
                "base_url must start with 'http://' or 'https://'."
            )

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

    def _run_business_rules(
        self,
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
        """Runs every business-rule validator for a create/update payload.

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
        self.validate_provider(provider)
        self.validate_integration_type(integration_type)
        self.validate_provider_type_pairing(provider, integration_type)
        self.validate_authentication_type(authentication_type, credentials)
        self.validate_configuration(provider, configuration)
        self.validate_webhook_url(webhook_url, integration_type)
        self.validate_base_url(base_url, integration_type)
        self.validate_timeout(timeout_seconds)
        self.validate_retry_count(retry_count)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    async def create_integration(
        self, payload: IntegrationCreate, *, created_by_id: Optional[int] = None
    ) -> Integration:
        """Validates and persists a new Integration.

        Args:
            payload: The validated `IntegrationCreate` request body.
            created_by_id: Identifier of the user creating this
                integration, if any (nullable for system-provisioned
                integrations).

        Returns:
            Integration: The newly created ORM instance.

        Raises:
            ValidationException: If any business-level field
                validation fails.
            BusinessRuleException: If the provider/type pairing,
                webhook/base URL requirement, or authentication/
                credential requirement is violated.
            DuplicateResourceException: If an integration with the
                same `name` already exists.
        """
        self._run_business_rules(
            provider=payload.provider,
            integration_type=payload.integration_type,
            authentication_type=payload.authentication_type,
            configuration=payload.configuration,
            credentials=payload.credentials,
            base_url=payload.base_url,
            webhook_url=payload.webhook_url,
            timeout_seconds=payload.timeout_seconds,
            retry_count=payload.retry_count,
        )

        existing = await self.repository.get_by_name(payload.name)
        if existing is not None:
            raise DuplicateResourceException(
                f"An integration named '{payload.name}' already exists."
            )

        data = payload.model_dump(exclude_unset=False)
        data["created_by_id"] = created_by_id

        integration = await self.repository.create(data)

        if integration.is_default:
            await self.repository.clear_default_for_type(
                integration.integration_type, exclude_id=integration.id
            )

        return integration

    async def get_integration(self, integration_id: uuid.UUID) -> Integration:
        """Fetches a single, non-deleted integration by id.

        Args:
            integration_id: Surrogate primary key of the integration.

        Returns:
            Integration: The requested ORM instance.

        Raises:
            NotFoundException: If no such integration exists.
        """
        integration = await self.repository.get_by_id(integration_id)
        if integration is None:
            raise NotFoundException(
                f"Integration '{integration_id}' was not found."
            )
        return integration

    async def update_integration(
        self, integration_id: uuid.UUID, payload: IntegrationUpdate
    ) -> Integration:
        """Validates and applies a partial update to an existing Integration.

        Args:
            integration_id: Surrogate primary key of the integration
                to update.
            payload: The validated `IntegrationUpdate` request body.

        Returns:
            Integration: The updated ORM instance.

        Raises:
            NotFoundException: If no such integration exists.
            ValidationException: If any business-level field
                validation fails.
            BusinessRuleException: If a resulting field combination
                (e.g. authentication type vs. credentials, or webhook/
                base URL requirement) is violated.
            DuplicateResourceException: If renaming would collide with
                another integration's `name`.
        """
        integration = await self.get_integration(integration_id)
        update_data = payload.model_dump(exclude_unset=True)

        if "name" in update_data and update_data["name"] != integration.name:
            existing = await self.repository.get_by_name(update_data["name"])
            if existing is not None and existing.id != integration.id:
                raise DuplicateResourceException(
                    f"An integration named '{update_data['name']}' already exists."
                )

        resulting_authentication_type = update_data.get(
            "authentication_type", integration.authentication_type
        )
        resulting_credentials = (
            update_data["credentials"]
            if "credentials" in update_data
            else integration.credentials
        )
        self.validate_authentication_type(
            resulting_authentication_type, resulting_credentials
        )

        resulting_configuration = update_data.get(
            "configuration", integration.configuration
        )
        self.validate_configuration(integration.provider, resulting_configuration)

        resulting_webhook_url = update_data.get(
            "webhook_url", integration.webhook_url
        )
        self.validate_webhook_url(resulting_webhook_url, integration.integration_type)

        resulting_base_url = update_data.get("base_url", integration.base_url)
        self.validate_base_url(resulting_base_url, integration.integration_type)

        resulting_timeout = update_data.get(
            "timeout_seconds", integration.timeout_seconds
        )
        self.validate_timeout(resulting_timeout)

        resulting_retry_count = update_data.get(
            "retry_count", integration.retry_count
        )
        self.validate_retry_count(resulting_retry_count)

        updated = await self.repository.update(integration, update_data)

        if update_data.get("is_default") is True:
            await self.repository.clear_default_for_type(
                updated.integration_type, exclude_id=updated.id
            )

        return updated

    async def delete_integration(self, integration_id: uuid.UUID) -> Integration:
        """Soft-deletes an integration.

        Args:
            integration_id: Surrogate primary key of the integration
                to delete.

        Returns:
            Integration: The soft-deleted ORM instance.

        Raises:
            NotFoundException: If no such integration exists.
        """
        integration = await self.get_integration(integration_id)
        return await self.repository.soft_delete(integration)

    async def restore_integration(self, integration_id: uuid.UUID) -> Integration:
        """Restores a previously soft-deleted integration.

        Args:
            integration_id: Surrogate primary key of the integration
                to restore.

        Returns:
            Integration: The restored ORM instance.

        Raises:
            NotFoundException: If no such integration exists at all
                (deleted or otherwise).
            ConflictException: If the integration is not currently
                soft-deleted.
        """
        integration = await self.repository.get_by_id(
            integration_id, include_deleted=True
        )
        if integration is None:
            raise NotFoundException(
                f"Integration '{integration_id}' was not found."
            )
        if not integration.is_deleted:
            raise ConflictException(
                f"Integration '{integration_id}' is not deleted."
            )
        return await self.repository.restore(integration)

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------
    async def enable_integration(self, integration_id: uuid.UUID) -> Integration:
        """Transitions an integration to `ACTIVE`.

        Args:
            integration_id: Surrogate primary key of the integration
                to enable.

        Returns:
            Integration: The updated ORM instance.

        Raises:
            NotFoundException: If no such integration exists.
            ConflictException: If the integration is already `ACTIVE`.
        """
        integration = await self.get_integration(integration_id)
        if integration.status == IntegrationStatus.ACTIVE:
            raise ConflictException(
                f"Integration '{integration_id}' is already active."
            )
        return await self.repository.enable(integration)

    async def disable_integration(self, integration_id: uuid.UUID) -> Integration:
        """Transitions an integration to `INACTIVE`.

        Args:
            integration_id: Surrogate primary key of the integration
                to disable.

        Returns:
            Integration: The updated ORM instance.

        Raises:
            NotFoundException: If no such integration exists.
            ConflictException: If the integration is already `INACTIVE`.
        """
        integration = await self.get_integration(integration_id)
        if integration.status == IntegrationStatus.INACTIVE:
            raise ConflictException(
                f"Integration '{integration_id}' is already inactive."
            )
        return await self.repository.disable(integration)

    async def update_status(
        self, integration_id: uuid.UUID, payload: IntegrationStatusUpdate
    ) -> Integration:
        """Applies an arbitrary status transition to an integration.

        Args:
            integration_id: Surrogate primary key of the integration.
            payload: The requested status transition and optional
                audit reason.

        Returns:
            Integration: The updated ORM instance.

        Raises:
            NotFoundException: If no such integration exists.
            ConflictException: If the integration is already in the
                requested status.
        """
        integration = await self.get_integration(integration_id)
        if integration.status == payload.status:
            raise ConflictException(
                f"Integration '{integration_id}' is already in status "
                f"'{payload.status.value}'."
            )
        return await self.repository.set_status(integration, payload.status)

    # ------------------------------------------------------------------
    # Connection test / health check
    # ------------------------------------------------------------------
    async def test_connection(self, integration_id: uuid.UUID) -> IntegrationHealthCheck:
        """Performs a structural readiness check ("connection test").

        This validates that the integration currently has everything
        it would need to attempt a live outbound call -- required
        `configuration` keys for its provider, a `base_url`/
        `webhook_url` where its `integration_type` requires one, and
        credentials where its `authentication_type` requires them.

        It does NOT perform actual network I/O against the external
        provider: doing so requires a provider-specific adapter/SDK
        client that lives in `app.utils` (out of scope for this
        service). Wire an adapter call into this method (or into
        `perform_health_check`) to upgrade this from a structural
        check into a live connectivity test; at that point a failed
        live call should raise `ExternalServiceException` rather than
        being swallowed.

        Args:
            integration_id: Surrogate primary key of the integration
                to test.

        Returns:
            IntegrationHealthCheck: The outcome of the readiness check.
            `is_healthy=True` means the integration is structurally
            ready to attempt a connection, not that connectivity to
            the provider has been confirmed.

        Raises:
            NotFoundException: If no such integration exists.
        """
        integration = await self.get_integration(integration_id)
        checked_at = datetime.now(timezone.utc)

        try:
            self.validate_authentication_type(
                integration.authentication_type, integration.credentials
            )
            self.validate_configuration(
                integration.provider, integration.configuration
            )
            self.validate_webhook_url(
                integration.webhook_url, integration.integration_type
            )
            self.validate_base_url(integration.base_url, integration.integration_type)
        except (ValidationException, BusinessRuleException) as exc:
            return IntegrationHealthCheck(
                integration_id=integration.id,
                is_healthy=False,
                status=IntegrationStatus.FAILED,
                checked_at=checked_at,
                latency_ms=None,
                message=exc.message,
            )

        return IntegrationHealthCheck(
            integration_id=integration.id,
            is_healthy=True,
            status=IntegrationStatus.ACTIVE,
            checked_at=checked_at,
            latency_ms=None,
            message="Integration is structurally ready; live connectivity was not tested.",
        )

    async def perform_health_check(
        self, integration_id: uuid.UUID
    ) -> IntegrationHealthCheck:
        """Runs a connection test and persists its outcome on the integration.

        Args:
            integration_id: Surrogate primary key of the integration
                to health-check.

        Returns:
            IntegrationHealthCheck: The outcome of the health check,
            reflecting the status that was persisted.

        Raises:
            NotFoundException: If no such integration exists.
        """
        outcome = await self.test_connection(integration_id)
        integration = await self.get_integration(integration_id)
        await self.repository.update_health_check_status(
            integration, status=outcome.status, checked_at=outcome.checked_at
        )
        return outcome

    async def update_sync_status(
        self,
        integration_id: uuid.UUID,
        *,
        synced_at: Optional[datetime] = None,
    ) -> Integration:
        """Records a successful data sync/exchange for an integration.

        Args:
            integration_id: Surrogate primary key of the integration.
            synced_at: Timestamp of the sync; defaults to the current
                UTC time.

        Returns:
            Integration: The updated ORM instance.

        Raises:
            NotFoundException: If no such integration exists.
        """
        integration = await self.get_integration(integration_id)
        return await self.repository.update_last_sync(
            integration, synced_at=synced_at
        )

    # ------------------------------------------------------------------
    # Listing / searching / filtering / pagination
    # ------------------------------------------------------------------
    async def list_integrations(
        self,
        *,
        filters: Optional[IntegrationFilter] = None,
        pagination: Optional[IntegrationPaginationParams] = None,
        sorting: Optional[IntegrationSortingParams] = None,
    ) -> IntegrationListResponse:
        """Lists integrations, filtered/sorted/paginated per the request.

        Args:
            filters: Optional structured filter criteria.
            pagination: Optional pagination parameters; defaults to
                page 1 / page_size 20.
            sorting: Optional sort parameters; defaults to
                `created_at` descending.

        Returns:
            IntegrationListResponse: The paginated listing.

        Raises:
            NotFoundException: If the requested page is beyond the
                last available page (and at least one record exists).
        """
        pagination = pagination or IntegrationPaginationParams()
        sorting = sorting or IntegrationSortingParams()
        filters = filters or IntegrationFilter()

        items, total = await self.repository.list_integrations(
            integration_type=filters.integration_type,
            provider=filters.provider,
            status=filters.status,
            authentication_type=filters.authentication_type,
            is_default=filters.is_default,
            search=filters.search,
            created_from=filters.created_from,
            created_to=filters.created_to,
            page=pagination.page,
            page_size=pagination.page_size,
            sort_by=sorting.sort_by,
            sort_order=sorting.sort_order,
        )

        total_pages = (
            (total + pagination.page_size - 1) // pagination.page_size
            if pagination.page_size
            else 0
        )
        if total > 0 and pagination.page > total_pages:
            raise NotFoundException(
                f"Page {pagination.page} does not exist; only "
                f"{total_pages} page(s) of integrations are available."
            )

        response_items = [
            IntegrationResponse.model_validate(item).model_copy(
                update={"has_credentials": item.credentials is not None}
            )
            for item in items
        ]

        return IntegrationListResponse(
            items=response_items,
            total=total,
            page=pagination.page,
            page_size=pagination.page_size,
            total_pages=total_pages,
        )

    async def search_integrations(
        self, term: str, *, limit: int = 20
    ) -> list[Integration]:
        """Performs a lightweight free-text search over integration names.

        Args:
            term: The free-text search term.
            limit: Maximum number of rows to return.

        Returns:
            list[Integration]: The matching, non-deleted integrations,
            most recently created first.

        Raises:
            ValidationException: If `term` is blank.
        """
        stripped = term.strip()
        if not stripped:
            raise ValidationException("Search term must not be blank.")
        return await self.repository.search_integrations(stripped, limit=limit)

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------
    async def bulk_enable(self, ids: list[uuid.UUID]) -> int:
        """Sets status to `ACTIVE` for a bounded set of integrations.

        Args:
            ids: The primary keys of the integrations to enable.

        Returns:
            int: The number of integrations affected.

        Raises:
            ValidationException: If `ids` is empty.
        """
        if not ids:
            raise ValidationException("At least one integration id is required.")
        return await self.repository.bulk_enable(ids)

    async def bulk_disable(self, ids: list[uuid.UUID]) -> int:
        """Sets status to `INACTIVE` for a bounded set of integrations.

        Args:
            ids: The primary keys of the integrations to disable.

        Returns:
            int: The number of integrations affected.

        Raises:
            ValidationException: If `ids` is empty.
        """
        if not ids:
            raise ValidationException("At least one integration id is required.")
        return await self.repository.bulk_disable(ids)

    async def bulk_delete(self, ids: list[uuid.UUID]) -> int:
        """Soft-deletes a bounded set of integrations.

        Args:
            ids: The primary keys of the integrations to soft-delete.

        Returns:
            int: The number of integrations affected.

        Raises:
            ValidationException: If `ids` is empty.
        """
        if not ids:
            raise ValidationException("At least one integration id is required.")
        return await self.repository.bulk_delete(ids)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    async def get_statistics(self) -> IntegrationStatisticsResponse:
        """Computes aggregate statistics over all non-deleted integrations.

        Returns:
            IntegrationStatisticsResponse: The computed aggregate
            statistics.
        """
        raw = await self.repository.get_statistics()
        return IntegrationStatisticsResponse(
            total_integrations=raw["total_integrations"],
            by_type=raw["by_type"],
            by_provider=raw["by_provider"],
            by_status=raw["by_status"],
            by_authentication_type=raw["by_authentication_type"],
            active_count=raw["active_count"],
            failed_count=raw["failed_count"],
            default_count=raw["default_count"],
            last_sync_at=raw["last_sync_at"],
            last_health_check_at=raw["last_health_check_at"],
        )

    async def count_by_provider(self) -> dict[str, int]:
        """Counts non-deleted integrations grouped by provider.

        Returns:
            dict[str, int]: Mapping of provider value to row count.
        """
        return await self.repository.count_by_provider()

    async def count_by_status(self) -> dict[str, int]:
        """Counts non-deleted integrations grouped by status.

        Returns:
            dict[str, int]: Mapping of status value to row count.
        """
        return await self.repository.count_by_status()

    async def count_by_type(self) -> dict[str, int]:
        """Counts non-deleted integrations grouped by integration type.

        Returns:
            dict[str, int]: Mapping of integration type value to row count.
        """
        return await self.repository.count_by_type()