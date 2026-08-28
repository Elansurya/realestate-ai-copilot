"""
backend/tests/test_integration_service.py

Tests for `app.services.integration_service.IntegrationService`.

Verified against: backend/app/services/integration_service.py,
backend/app/schemas/integration.py, backend/app/models/integration.py.

The repository is fully mocked (`unittest.mock.AsyncMock`) so these
tests exercise only the service's own business-rule validation and
orchestration logic -- not real persistence. No repository or service
method used below is invented; each corresponds to a method that
exists verbatim in `IntegrationRepository` / `IntegrationService`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.exceptions import (
    BusinessRuleException,
    ConflictException,
    DuplicateResourceException,
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
from app.schemas.integration import (
    IntegrationCreate,
    IntegrationFilter,
    IntegrationPaginationParams,
    IntegrationStatusUpdate,
    IntegrationUpdate,
)
from app.services.integration_service import IntegrationService

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_integration(**overrides) -> Integration:
    """Builds an in-memory `Integration` ORM instance for mock returns.

    `created_at`/`updated_at` default to the current UTC time (rather
    than being left unset/`None`) because several tests in this file
    (e.g. `TestListAndSearch.test_list_integrations_returns_populated_response`)
    feed the resulting instance through
    `IntegrationResponse.model_validate(...)`, whose `created_at`/
    `updated_at` fields are required, non-optional `datetime` values
    (see `app/schemas/integration.py`) -- mirroring the real ORM model,
    where both columns are `Mapped[datetime]` with `nullable=False`
    (see `app/models/integration.py`), i.e. a genuinely persisted
    `Integration` row could never actually have a `None` here.

    Args:
        **overrides: Attribute overrides applied after defaults.

    Returns:
        Integration: An unpersisted ORM instance (not flushed/committed).
    """
    now = datetime.now(timezone.utc)
    integration = Integration(
        id=overrides.pop("id", uuid.uuid4()),
        name=overrides.pop("name", "Primary SMTP"),
        provider=overrides.pop("provider", IntegrationProvider.SMTP),
        integration_type=overrides.pop("integration_type", IntegrationType.EMAIL),
        status=overrides.pop("status", IntegrationStatus.PENDING_VERIFICATION),
        authentication_type=overrides.pop(
            "authentication_type", AuthenticationType.BASIC_AUTH
        ),
        configuration=overrides.pop(
            "configuration", {"host": "smtp.example.com", "port": 587}
        ),
        credentials=overrides.pop(
            "credentials", {"username": "user", "password": "pass"}
        ),
        base_url=overrides.pop("base_url", None),
        webhook_url=overrides.pop("webhook_url", None),
        timeout_seconds=overrides.pop("timeout_seconds", 30),
        retry_count=overrides.pop("retry_count", 3),
        rate_limit_per_minute=overrides.pop("rate_limit_per_minute", None),
        is_default=overrides.pop("is_default", False),
        is_deleted=overrides.pop("is_deleted", False),
        created_at=overrides.pop("created_at", now),
        updated_at=overrides.pop("updated_at", now),
    )
    for key, value in overrides.items():
        setattr(integration, key, value)
    return integration


def _create_payload(**overrides) -> IntegrationCreate:
    """Builds a valid `IntegrationCreate` payload for a generic SMTP integration.

    Args:
        **overrides: Field overrides applied on top of the defaults.

    Returns:
        IntegrationCreate: The validated creation payload.
    """
    data = {
        "name": "Primary SMTP",
        "provider": IntegrationProvider.SMTP,
        "integration_type": IntegrationType.EMAIL,
        "authentication_type": AuthenticationType.BASIC_AUTH,
        "configuration": {"host": "smtp.example.com", "port": 587},
        "credentials": {"username": "user", "password": "pass"},
    }
    data.update(overrides)
    return IntegrationCreate(**data)


@pytest.fixture
def repository() -> AsyncMock:
    """Builds a fully-mocked `IntegrationRepository`.

    Returns:
        AsyncMock: A mock exposing every `IntegrationRepository` method
        as an `AsyncMock`.
    """
    return AsyncMock()


@pytest.fixture
def service(repository: AsyncMock) -> IntegrationService:
    """Builds an `IntegrationService` bound to the mocked repository.

    Args:
        repository: The mocked repository fixture.

    Returns:
        IntegrationService: The service under test.
    """
    return IntegrationService(repository)


# ---------------------------------------------------------------------------
# Static validators
# ---------------------------------------------------------------------------
class TestStaticValidators:
    """Tests for `IntegrationService`'s standalone validator staticmethods."""

    def test_validate_provider_accepts_enum_member(self, service):
        """A valid `IntegrationProvider` member passes without raising."""
        IntegrationService.validate_provider(IntegrationProvider.STRIPE)

    def test_validate_provider_rejects_non_member(self, service):
        """A non-`IntegrationProvider` value raises `ValidationException`."""
        with pytest.raises(ValidationException):
            IntegrationService.validate_provider("not-a-provider")

    def test_validate_provider_type_pairing_accepts_matching_pair(self, service):
        """A correct provider/type pairing passes without raising."""
        IntegrationService.validate_provider_type_pairing(
            IntegrationProvider.STRIPE, IntegrationType.PAYMENT_GATEWAY
        )

    def test_validate_provider_type_pairing_rejects_mismatch(self, service):
        """An incorrect provider/type pairing raises `BusinessRuleException`."""
        with pytest.raises(BusinessRuleException):
            IntegrationService.validate_provider_type_pairing(
                IntegrationProvider.STRIPE, IntegrationType.EMAIL
            )

    def test_validate_authentication_type_requires_credentials(self, service):
        """A non-`NONE` auth type without credentials raises `BusinessRuleException`."""
        with pytest.raises(BusinessRuleException):
            IntegrationService.validate_authentication_type(
                AuthenticationType.API_KEY, None
            )

    def test_validate_authentication_type_allows_none_without_credentials(
        self, service
    ):
        """`AuthenticationType.NONE` never requires credentials."""
        IntegrationService.validate_authentication_type(AuthenticationType.NONE, None)

    def test_validate_configuration_requires_provider_keys(self, service):
        """Missing a provider's required configuration key raises `ValidationException`."""
        with pytest.raises(ValidationException):
            IntegrationService.validate_configuration(IntegrationProvider.AWS_S3, {})

    def test_validate_configuration_passes_when_keys_present(self, service):
        """Supplying all required configuration keys passes without raising."""
        IntegrationService.validate_configuration(
            IntegrationProvider.AWS_S3, {"bucket_name": "b", "region": "us-east-1"}
        )

    def test_validate_webhook_url_required_for_webhook_type(self, service):
        """`IntegrationType.WEBHOOK` without a `webhook_url` raises `BusinessRuleException`."""
        with pytest.raises(BusinessRuleException):
            IntegrationService.validate_webhook_url(None, IntegrationType.WEBHOOK)

    def test_validate_webhook_url_rejects_bad_scheme(self, service):
        """A `webhook_url` without an http(s) scheme raises `ValidationException`."""
        with pytest.raises(ValidationException):
            IntegrationService.validate_webhook_url(
                "ftp://bad.example.com", IntegrationType.WEBHOOK
            )

    def test_validate_base_url_required_for_custom_api_type(self, service):
        """`IntegrationType.CUSTOM_API` without a `base_url` raises `BusinessRuleException`."""
        with pytest.raises(BusinessRuleException):
            IntegrationService.validate_base_url(None, IntegrationType.CUSTOM_API)

    def test_validate_timeout_enforces_bounds(self, service):
        """`timeout_seconds` outside `[1, 300]` raises `ValidationException`."""
        with pytest.raises(ValidationException):
            IntegrationService.validate_timeout(0)
        with pytest.raises(ValidationException):
            IntegrationService.validate_timeout(301)

    def test_validate_retry_count_enforces_bounds(self, service):
        """`retry_count` outside `[0, 10]` raises `ValidationException`."""
        with pytest.raises(ValidationException):
            IntegrationService.validate_retry_count(-1)
        with pytest.raises(ValidationException):
            IntegrationService.validate_retry_count(11)


# ---------------------------------------------------------------------------
# create_integration
# ---------------------------------------------------------------------------
class TestCreateIntegration:
    """Tests for `IntegrationService.create_integration`."""

    async def test_create_integration_persists_via_repository(
        self, service, repository
    ):
        """A valid payload is persisted via `repository.create`."""
        repository.get_by_name.return_value = None
        repository.create.return_value = _make_integration()

        result = await service.create_integration(_create_payload(), created_by_id=7)

        repository.get_by_name.assert_awaited_once_with("Primary SMTP")
        repository.create.assert_awaited_once()
        assert result.name == "Primary SMTP"

    async def test_create_integration_raises_on_duplicate_name(
        self, service, repository
    ):
        """An existing row with the same name raises `DuplicateResourceException`."""
        repository.get_by_name.return_value = _make_integration()

        with pytest.raises(DuplicateResourceException):
            await service.create_integration(_create_payload())

        repository.create.assert_not_awaited()

    async def test_create_integration_clears_prior_default_when_promoted(
        self, service, repository
    ):
        """Creating a default integration clears any other default of the same type."""
        repository.get_by_name.return_value = None
        created = _make_integration(is_default=True)
        repository.create.return_value = created

        await service.create_integration(_create_payload(is_default=True))

        repository.clear_default_for_type.assert_awaited_once_with(
            created.integration_type, exclude_id=created.id
        )

    def test_create_payload_rejects_provider_type_mismatch(
        self, service, repository
    ):
        """A mismatched provider/type pairing is rejected at schema construction.

        `IntegrationCreate` (see `app/schemas/integration.py`) has its
        own `model_validator` (`_validate_provider_type_consistency`)
        that enforces this exact rule at payload-construction time,
        using the same `_PROVIDER_TYPE_MAP` the service re-checks via
        `IntegrationService.validate_provider_type_pairing`. Because
        the schema validates this first, `_create_payload(...)` itself
        raises `pydantic.ValidationError` here -- the service method
        is never reached with an invalid combination, so there is no
        way to observe it raising `BusinessRuleException` for this via
        the public `create_integration` path. The service-level rule
        itself (used for combinations the schema layer cannot see,
        e.g. re-validating a to-be-persisted dict from multiple
        sources) is exercised directly in
        `TestStaticValidators.test_validate_provider_type_pairing_rejects_mismatch`.
        """
        with pytest.raises(PydanticValidationError):
            _create_payload(
                provider=IntegrationProvider.STRIPE,
                integration_type=IntegrationType.EMAIL,
            )

        repository.get_by_name.assert_not_awaited()
        repository.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_integration
# ---------------------------------------------------------------------------
class TestGetIntegration:
    """Tests for `IntegrationService.get_integration`."""

    async def test_get_integration_returns_repository_result(
        self, service, repository
    ):
        """Returns the ORM instance returned by `repository.get_by_id`."""
        integration = _make_integration()
        repository.get_by_id.return_value = integration

        result = await service.get_integration(integration.id)

        assert result is integration

    async def test_get_integration_raises_not_found(self, service, repository):
        """Raises `NotFoundException` when `repository.get_by_id` returns `None`."""
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.get_integration(uuid.uuid4())


# ---------------------------------------------------------------------------
# update_integration
# ---------------------------------------------------------------------------
class TestUpdateIntegration:
    """Tests for `IntegrationService.update_integration`."""

    async def test_update_integration_applies_partial_changes(
        self, service, repository
    ):
        """A partial update is forwarded to `repository.update` with only set fields."""
        existing = _make_integration()
        repository.get_by_id.return_value = existing
        repository.update.return_value = existing

        await service.update_integration(
            existing.id, IntegrationUpdate(timeout_seconds=99)
        )

        repository.update.assert_awaited_once()
        args, _ = repository.update.call_args
        assert args[1] == {"timeout_seconds": 99}

    async def test_update_integration_raises_not_found(self, service, repository):
        """Raises `NotFoundException` when the integration does not exist."""
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.update_integration(uuid.uuid4(), IntegrationUpdate())

    async def test_update_integration_raises_on_name_collision(
        self, service, repository
    ):
        """Renaming to a name already used by another row raises `DuplicateResourceException`."""
        existing = _make_integration(name="Original Name")
        other = _make_integration(name="Taken Name")
        repository.get_by_id.return_value = existing
        repository.get_by_name.return_value = other

        with pytest.raises(DuplicateResourceException):
            await service.update_integration(
                existing.id, IntegrationUpdate(name="Taken Name")
            )

    async def test_update_integration_allows_rename_to_same_id(
        self, service, repository
    ):
        """Renaming does not collide with the integration's own existing name."""
        existing = _make_integration(name="Same Name")
        repository.get_by_id.return_value = existing
        repository.get_by_name.return_value = existing
        repository.update.return_value = existing

        await service.update_integration(
            existing.id, IntegrationUpdate(name="Same Name")
        )

        repository.update.assert_awaited_once()

    def test_update_payload_rejects_explicit_auth_type_change_without_credentials(
        self, service, repository
    ):
        """Explicitly clearing credentials while switching auth type is rejected at schema level.

        `IntegrationUpdate` (see `app/schemas/integration.py`) has its
        own `model_validator` (`_validate_auth_requires_credentials`)
        that raises whenever `authentication_type` is being set to a
        non-`NONE` value AND `credentials` is *explicitly* present in
        the request (`"credentials" in self.model_fields_set`) but
        falsy. Passing `credentials=None` explicitly, as this test
        originally did, trips that schema-level check immediately at
        `IntegrationUpdate(...)` construction, raising
        `pydantic.ValidationError` before the payload could ever reach
        `service.update_integration`.
        """
        with pytest.raises(PydanticValidationError):
            IntegrationUpdate(
                authentication_type=AuthenticationType.API_KEY, credentials=None
            )

        repository.get_by_id.assert_not_awaited()

    async def test_update_integration_rejects_auth_type_change_against_existing_credentials(
        self, service, repository
    ):
        """Switching auth type is rejected using the existing row's credentials.

        Here `credentials` is omitted entirely from the update payload
        (not merely set to `None`), so it is absent from
        `model_fields_set` and the schema-level validator above does
        not fire -- `IntegrationUpdate(authentication_type=...)`
        constructs successfully. This is exactly the case the
        service's own defense-in-depth check
        (`update_integration`'s `resulting_credentials` computation
        over `integration.credentials` when `"credentials" not in
        update_data`) exists for: the existing, persisted integration
        has no credentials on file, and the caller is switching it to
        an authentication type that requires them without supplying
        new ones.
        """
        existing = _make_integration(
            authentication_type=AuthenticationType.NONE, credentials=None
        )
        repository.get_by_id.return_value = existing

        payload = IntegrationUpdate(authentication_type=AuthenticationType.API_KEY)

        with pytest.raises(BusinessRuleException):
            await service.update_integration(existing.id, payload)


# ---------------------------------------------------------------------------
# delete_integration / restore_integration
# ---------------------------------------------------------------------------
class TestDeleteAndRestore:
    """Tests for `IntegrationService.delete_integration`/`restore_integration`."""

    async def test_delete_integration_soft_deletes(self, service, repository):
        """`delete_integration` soft-deletes an existing row via the repository."""
        existing = _make_integration()
        repository.get_by_id.return_value = existing
        repository.soft_delete.return_value = existing

        await service.delete_integration(existing.id)

        repository.soft_delete.assert_awaited_once_with(existing)

    async def test_delete_integration_raises_not_found(self, service, repository):
        """`delete_integration` raises `NotFoundException` for an unknown id."""
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.delete_integration(uuid.uuid4())

    async def test_restore_integration_restores_deleted_row(self, service, repository):
        """`restore_integration` restores a row currently soft-deleted."""
        existing = _make_integration(is_deleted=True)
        repository.get_by_id.return_value = existing
        repository.restore.return_value = existing

        await service.restore_integration(existing.id)

        repository.restore.assert_awaited_once_with(existing)

    async def test_restore_integration_raises_not_found_when_missing(
        self, service, repository
    ):
        """`restore_integration` raises `NotFoundException` when no row exists at all."""
        repository.get_by_id.return_value = None

        with pytest.raises(NotFoundException):
            await service.restore_integration(uuid.uuid4())

    async def test_restore_integration_raises_conflict_when_not_deleted(
        self, service, repository
    ):
        """`restore_integration` raises `ConflictException` for a non-deleted row."""
        existing = _make_integration(is_deleted=False)
        repository.get_by_id.return_value = existing

        with pytest.raises(ConflictException):
            await service.restore_integration(existing.id)


# ---------------------------------------------------------------------------
# enable / disable / update_status
# ---------------------------------------------------------------------------
class TestEnableDisableStatus:
    """Tests for `enable_integration`/`disable_integration`/`update_status`."""

    async def test_enable_integration_transitions_to_active(
        self, service, repository
    ):
        """`enable_integration` calls `repository.enable` for an inactive row."""
        existing = _make_integration(status=IntegrationStatus.INACTIVE)
        repository.get_by_id.return_value = existing
        repository.enable.return_value = existing

        await service.enable_integration(existing.id)

        repository.enable.assert_awaited_once_with(existing)

    async def test_enable_integration_raises_conflict_when_already_active(
        self, service, repository
    ):
        """`enable_integration` raises `ConflictException` when already `ACTIVE`."""
        existing = _make_integration(status=IntegrationStatus.ACTIVE)
        repository.get_by_id.return_value = existing

        with pytest.raises(ConflictException):
            await service.enable_integration(existing.id)

    async def test_disable_integration_raises_conflict_when_already_inactive(
        self, service, repository
    ):
        """`disable_integration` raises `ConflictException` when already `INACTIVE`."""
        existing = _make_integration(status=IntegrationStatus.INACTIVE)
        repository.get_by_id.return_value = existing

        with pytest.raises(ConflictException):
            await service.disable_integration(existing.id)

    async def test_update_status_applies_new_status(self, service, repository):
        """`update_status` forwards a differing status transition to the repository."""
        existing = _make_integration(status=IntegrationStatus.PENDING_VERIFICATION)
        repository.get_by_id.return_value = existing
        repository.set_status.return_value = existing

        await service.update_status(
            existing.id, IntegrationStatusUpdate(status=IntegrationStatus.FAILED)
        )

        repository.set_status.assert_awaited_once_with(
            existing, IntegrationStatus.FAILED
        )

    async def test_update_status_raises_conflict_when_unchanged(
        self, service, repository
    ):
        """`update_status` raises `ConflictException` when the status is unchanged."""
        existing = _make_integration(status=IntegrationStatus.FAILED)
        repository.get_by_id.return_value = existing

        with pytest.raises(ConflictException):
            await service.update_status(
                existing.id, IntegrationStatusUpdate(status=IntegrationStatus.FAILED)
            )


# ---------------------------------------------------------------------------
# test_connection / perform_health_check
# ---------------------------------------------------------------------------
class TestConnectionAndHealthCheck:
    """Tests for `test_connection`/`perform_health_check`."""

    async def test_test_connection_reports_healthy_when_structurally_ready(
        self, service, repository
    ):
        """A fully-configured integration reports `is_healthy=True`."""
        existing = _make_integration()
        repository.get_by_id.return_value = existing

        outcome = await service.test_connection(existing.id)

        assert outcome.is_healthy is True
        assert outcome.status == IntegrationStatus.ACTIVE

    async def test_test_connection_reports_unhealthy_on_validation_failure(
        self, service, repository
    ):
        """A structurally invalid integration reports `is_healthy=False`, not an exception."""
        existing = _make_integration(
            provider=IntegrationProvider.AWS_S3,
            integration_type=IntegrationType.STORAGE,
            configuration={},
        )
        repository.get_by_id.return_value = existing

        outcome = await service.test_connection(existing.id)

        assert outcome.is_healthy is False
        assert outcome.status == IntegrationStatus.FAILED
        assert outcome.message is not None

    async def test_perform_health_check_persists_outcome(self, service, repository):
        """`perform_health_check` persists the resulting status via the repository."""
        existing = _make_integration()
        repository.get_by_id.return_value = existing

        outcome = await service.perform_health_check(existing.id)

        repository.update_health_check_status.assert_awaited_once()
        _, kwargs = repository.update_health_check_status.call_args
        assert kwargs["status"] == outcome.status


# ---------------------------------------------------------------------------
# list_integrations / search_integrations
# ---------------------------------------------------------------------------
class TestListAndSearch:
    """Tests for `list_integrations`/`search_integrations`."""

    async def test_list_integrations_returns_populated_response(
        self, service, repository
    ):
        """`list_integrations` wraps repository results into an `IntegrationListResponse`."""
        integration = _make_integration()
        repository.list_integrations.return_value = ([integration], 1)

        result = await service.list_integrations(
            pagination=IntegrationPaginationParams(page=1, page_size=20)
        )

        assert result.total == 1
        assert result.page == 1
        assert result.total_pages == 1

    async def test_list_integrations_raises_not_found_beyond_last_page(
        self, service, repository
    ):
        """Requesting a page beyond the last available page raises `NotFoundException`."""
        repository.list_integrations.return_value = ([], 1)

        with pytest.raises(NotFoundException):
            await service.list_integrations(
                pagination=IntegrationPaginationParams(page=5, page_size=20)
            )

    async def test_search_integrations_rejects_blank_term(self, service, repository):
        """`search_integrations` raises `ValidationException` for a blank term."""
        with pytest.raises(ValidationException):
            await service.search_integrations("   ")

        repository.search_integrations.assert_not_awaited()

    async def test_search_integrations_delegates_to_repository(
        self, service, repository
    ):
        """`search_integrations` forwards the stripped term to the repository."""
        repository.search_integrations.return_value = []

        await service.search_integrations("  term  ")

        repository.search_integrations.assert_awaited_once_with("term", limit=20)


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------
class TestBulkOperations:
    """Tests for `bulk_enable`/`bulk_disable`/`bulk_delete`."""

    async def test_bulk_enable_rejects_empty_ids(self, service, repository):
        """`bulk_enable` raises `ValidationException` for an empty id list."""
        with pytest.raises(ValidationException):
            await service.bulk_enable([])

        repository.bulk_enable.assert_not_awaited()

    async def test_bulk_disable_delegates_to_repository(self, service, repository):
        """`bulk_disable` forwards the id list to the repository."""
        ids = [uuid.uuid4()]
        repository.bulk_disable.return_value = 1

        result = await service.bulk_disable(ids)

        repository.bulk_disable.assert_awaited_once_with(ids)
        assert result == 1

    async def test_bulk_delete_rejects_empty_ids(self, service, repository):
        """`bulk_delete` raises `ValidationException` for an empty id list."""
        with pytest.raises(ValidationException):
            await service.bulk_delete([])


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
class TestStatistics:
    """Tests for `get_statistics`/`count_by_*`."""

    async def test_get_statistics_maps_repository_output(self, service, repository):
        """`get_statistics` maps `repository.get_statistics` output onto the response schema."""
        repository.get_statistics.return_value = {
            "total_integrations": 5,
            "by_type": {"email": 5},
            "by_provider": {"smtp": 5},
            "by_status": {"active": 5},
            "by_authentication_type": {"basic_auth": 5},
            "active_count": 5,
            "failed_count": 0,
            "default_count": 1,
            "last_sync_at": None,
            "last_health_check_at": None,
        }

        result = await service.get_statistics()

        assert result.total_integrations == 5
        assert result.active_count == 5

    async def test_count_by_provider_delegates_to_repository(
        self, service, repository
    ):
        """`count_by_provider` delegates directly to the repository method."""
        repository.count_by_provider.return_value = {"smtp": 1}

        result = await service.count_by_provider()

        assert result == {"smtp": 1}