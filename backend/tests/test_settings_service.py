# backend/tests/test_settings_service.py

"""
Settings Module - Phase 4
Service Layer Test Suite

Covers:
    - setting_key normalization / validation
    - Value-vs-data_type structural validation (all SettingDataType members)
    - Custom validation_rules engine (enum, min, max, min_length, max_length, pattern)
    - Encrypted/public mutual exclusion
    - Editability guard
    - Protected system setting guard
    - Create / Get / Update / Delete
    - List / Search
    - Scoped lookups (public / editable / encrypted)
    - Bulk update / Bulk delete (including batch-size limits)
    - Statistics
    - Cache snapshot construction (redaction of encrypted values)
    - Domain exception status codes

These tests exercise `SettingsService` in isolation, with the
repository layer fully mocked via `AsyncMock`, mirroring the
conventions established in `test_audit_service.py` /
`test_payment_service.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import (
    BusinessRuleException,
    DuplicateResourceException,
    NotFoundException,
    ValidationException,
)
from app.models.settings import SettingCategory, SettingDataType
from app.schemas.settings import (
    SettingsCreate,
    SettingsFilter,
    SettingsUpdate,
)
from app.services.settings_service import SettingsService

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Helpers / Fixtures
# --------------------------------------------------------------------------- #

def make_setting(
    setting_id=None,
    category=SettingCategory.EMAIL,
    setting_key="SMTP_HOST",
    setting_value="smtp.example.com",
    description="SMTP server hostname.",
    data_type=SettingDataType.STRING,
    is_public=False,
    is_editable=True,
    is_encrypted=False,
    validation_rules=None,
    created_by=1,
    updated_by=1,
    created_at=None,
    updated_at=None,
):
    entry = MagicMock()
    entry.id = setting_id or uuid.uuid4()
    entry.category = category
    entry.setting_key = setting_key
    entry.setting_value = setting_value
    entry.description = description
    entry.data_type = data_type
    entry.is_public = is_public
    entry.is_editable = is_editable
    entry.is_encrypted = is_encrypted
    entry.validation_rules = validation_rules
    entry.created_by = created_by
    entry.updated_by = updated_by
    entry.created_at = created_at or datetime.now(timezone.utc)
    entry.updated_at = updated_at or datetime.now(timezone.utc)
    return entry


@pytest.fixture
def service():
    repository = AsyncMock()
    return SettingsService(repository)


@pytest.fixture
def valid_create_payload():
    return SettingsCreate(
        category=SettingCategory.EMAIL,
        setting_key="SMTP_HOST",
        setting_value="smtp.example.com",
        description="SMTP server hostname.",
        data_type=SettingDataType.STRING,
        is_public=False,
        is_editable=True,
        is_encrypted=False,
        validation_rules=None,
        created_by=1,
    )


# --------------------------------------------------------------------------- #
# Create — validation and normalization
# --------------------------------------------------------------------------- #

class TestCreateSettingValidation:

    async def test_create_setting_success(self, service, valid_create_payload):
        service.repository.exists_by_category_and_key.return_value = False
        created = make_setting()
        service.repository.create.return_value = created

        result = await service.create_setting(valid_create_payload)

        assert result.setting_key == "SMTP_HOST"
        service.repository.create.assert_awaited_once()

    async def test_setting_key_is_trimmed_and_uppercased(self, service, valid_create_payload):
        valid_create_payload.setting_key = "  smtp_host  "
        service.repository.exists_by_category_and_key.return_value = False
        service.repository.create.return_value = make_setting()

        await service.create_setting(valid_create_payload)

        call_args = service.repository.create.call_args[0][0]
        assert call_args["setting_key"] == "SMTP_HOST"

    async def test_setting_key_too_long_rejected(self, service, valid_create_payload):
        valid_create_payload.setting_key = "A" * 151
        with pytest.raises(ValidationException):
            await service.create_setting(valid_create_payload)

    async def test_setting_key_bad_pattern_rejected(self, service, valid_create_payload):
        valid_create_payload.setting_key = "1_STARTS_WITH_DIGIT"
        with pytest.raises(ValidationException):
            await service.create_setting(valid_create_payload)

    async def test_setting_key_with_invalid_characters_rejected(self, service, valid_create_payload):
        valid_create_payload.setting_key = "BAD KEY WITH SPACES"
        with pytest.raises(ValidationException):
            await service.create_setting(valid_create_payload)

    async def test_duplicate_category_and_key_raises_duplicate_resource_exception(
        self, service, valid_create_payload
    ):
        service.repository.exists_by_category_and_key.return_value = True
        with pytest.raises(DuplicateResourceException):
            await service.create_setting(valid_create_payload)
        service.repository.create.assert_not_awaited()

    async def test_encrypted_and_public_rejected_at_service_layer(self, service):
        payload = SettingsCreate.model_construct(
            category=SettingCategory.SYSTEM,
            setting_key="BAD_FLAG_COMBO",
            setting_value="x",
            description=None,
            data_type=SettingDataType.STRING,
            is_public=True,
            is_editable=True,
            is_encrypted=True,
            validation_rules=None,
            created_by=1,
        )
        with pytest.raises(BusinessRuleException):
            await service.create_setting(payload)


# --------------------------------------------------------------------------- #
# Value-vs-data_type structural validation
# --------------------------------------------------------------------------- #

class TestValueAgainstDataType:

    @pytest.mark.parametrize(
        "data_type,value",
        [
            (SettingDataType.STRING, "hello"),
            (SettingDataType.INTEGER, 42),
            (SettingDataType.FLOAT, 3.14),
            (SettingDataType.BOOLEAN, True),
            (SettingDataType.JSON, {"a": 1}),
            (SettingDataType.ARRAY, [1, 2, 3]),
            (SettingDataType.DATE, "2026-08-02"),
            (SettingDataType.DATETIME, "2026-08-02T00:00:00"),
            (SettingDataType.EMAIL, "user@example.com"),
            (SettingDataType.URL, "https://example.com"),
            (SettingDataType.PASSWORD, "s3cr3t"),
        ],
    )
    async def test_valid_value_for_each_data_type_accepted(self, service, data_type, value):
        payload = SettingsCreate(
            category=SettingCategory.GENERAL,
            setting_key="TEST_KEY",
            setting_value=value,
            data_type=data_type,
        )
        service.repository.exists_by_category_and_key.return_value = False
        service.repository.create.return_value = make_setting(
            setting_value=value, data_type=data_type
        )
        result = await service.create_setting(payload)
        assert result.data_type == data_type

    @pytest.mark.parametrize(
        "data_type,value",
        [
            (SettingDataType.STRING, 123),
            (SettingDataType.INTEGER, "not-an-int"),
            (SettingDataType.INTEGER, True),  # bool must not satisfy INTEGER
            (SettingDataType.FLOAT, "not-a-float"),
            (SettingDataType.BOOLEAN, "true"),
            (SettingDataType.JSON, "not-json"),
            (SettingDataType.ARRAY, {"not": "a-list"}),
            (SettingDataType.DATE, "not-a-date"),
            (SettingDataType.DATETIME, "not-a-datetime"),
            (SettingDataType.EMAIL, "not-an-email"),
            (SettingDataType.URL, "not-a-url"),
            (SettingDataType.PASSWORD, ""),
        ],
    )
    async def test_invalid_value_for_each_data_type_rejected(self, service, data_type, value):
        payload = SettingsCreate(
            category=SettingCategory.GENERAL,
            setting_key="TEST_KEY",
            setting_value=value,
            data_type=data_type,
        )
        with pytest.raises(ValidationException):
            await service.create_setting(payload)

    async def test_none_value_bypasses_type_check(self, service):
        payload = SettingsCreate(
            category=SettingCategory.GENERAL,
            setting_key="NULLABLE_KEY",
            setting_value=None,
            data_type=SettingDataType.INTEGER,
        )
        service.repository.exists_by_category_and_key.return_value = False
        service.repository.create.return_value = make_setting(setting_value=None)
        result = await service.create_setting(payload)
        assert result is not None


# --------------------------------------------------------------------------- #
# validation_rules engine
# --------------------------------------------------------------------------- #

class TestValidationRulesEngine:

    async def test_enum_rule_accepts_allowed_value(self, service):
        payload = SettingsCreate(
            category=SettingCategory.THEME,
            setting_key="MODE",
            setting_value="dark",
            data_type=SettingDataType.STRING,
            validation_rules={"enum": ["light", "dark", "system"]},
        )
        service.repository.exists_by_category_and_key.return_value = False
        service.repository.create.return_value = make_setting(setting_value="dark")
        result = await service.create_setting(payload)
        assert result is not None

    async def test_enum_rule_rejects_disallowed_value(self, service):
        payload = SettingsCreate(
            category=SettingCategory.THEME,
            setting_key="MODE",
            setting_value="neon",
            data_type=SettingDataType.STRING,
            validation_rules={"enum": ["light", "dark", "system"]},
        )
        with pytest.raises(ValidationException):
            await service.create_setting(payload)

    async def test_min_max_rules_enforced_for_numeric_value(self, service):
        payload = SettingsCreate(
            category=SettingCategory.EMAIL,
            setting_key="SMTP_PORT",
            setting_value=99999,
            data_type=SettingDataType.INTEGER,
            validation_rules={"min": 1, "max": 65535},
        )
        with pytest.raises(ValidationException):
            await service.create_setting(payload)

    async def test_min_length_max_length_rules_enforced_for_string(self, service):
        payload = SettingsCreate(
            category=SettingCategory.GENERAL,
            setting_key="SHORT_CODE",
            setting_value="x",
            data_type=SettingDataType.STRING,
            validation_rules={"min_length": 3, "max_length": 10},
        )
        with pytest.raises(ValidationException):
            await service.create_setting(payload)

    async def test_pattern_rule_enforced(self, service):
        payload = SettingsCreate(
            category=SettingCategory.GENERAL,
            setting_key="SLUG",
            setting_value="Not A Slug!",
            data_type=SettingDataType.STRING,
            validation_rules={"pattern": r"^[a-z0-9\-]+$"},
        )
        with pytest.raises(ValidationException):
            await service.create_setting(payload)

    async def test_combined_rules_all_pass(self, service):
        payload = SettingsCreate(
            category=SettingCategory.GENERAL,
            setting_key="SLUG",
            setting_value="valid-slug-123",
            data_type=SettingDataType.STRING,
            validation_rules={"min_length": 3, "max_length": 32, "pattern": r"^[a-z0-9\-]+$"},
        )
        service.repository.exists_by_category_and_key.return_value = False
        service.repository.create.return_value = make_setting(setting_value="valid-slug-123")
        result = await service.create_setting(payload)
        assert result is not None


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #

class TestGetSetting:

    async def test_get_setting_by_id_success(self, service):
        entry = make_setting()
        service.repository.get_by_id.return_value = entry

        result = await service.get_setting(entry.id)
        assert result.id == entry.id

    async def test_get_setting_by_id_not_found_raises_not_found(self, service):
        service.repository.get_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.get_setting(uuid.uuid4())

    async def test_get_setting_by_key_success(self, service):
        entry = make_setting()
        service.repository.get_by_key.return_value = entry

        result = await service.get_setting_by_key("smtp_host")
        assert result.setting_key == "SMTP_HOST"

    async def test_get_setting_by_key_blank_raises_validation_exception(self, service):
        with pytest.raises(ValidationException):
            await service.get_setting_by_key("   ")

    async def test_get_setting_by_key_not_found_raises_not_found(self, service):
        service.repository.get_by_key.return_value = None
        with pytest.raises(NotFoundException):
            await service.get_setting_by_key("MISSING_KEY")

    async def test_get_setting_by_category_and_key_success(self, service):
        entry = make_setting()
        service.repository.get_by_category_and_key.return_value = entry

        result = await service.get_setting_by_category_and_key(
            SettingCategory.EMAIL, "smtp_host"
        )
        assert result.category == SettingCategory.EMAIL

    async def test_get_setting_by_category_and_key_not_found(self, service):
        service.repository.get_by_category_and_key.return_value = None
        with pytest.raises(NotFoundException):
            await service.get_setting_by_category_and_key(SettingCategory.EMAIL, "MISSING")

    async def test_get_settings_by_category_returns_list(self, service):
        entries = [make_setting(setting_key=f"KEY_{i}") for i in range(3)]
        service.repository.get_by_category.return_value = entries

        results = await service.get_settings_by_category(SettingCategory.EMAIL)
        assert len(results) == 3


# --------------------------------------------------------------------------- #
# Update
# --------------------------------------------------------------------------- #

class TestUpdateSetting:

    async def test_update_setting_success(self, service):
        entry = make_setting()
        service.repository.get_by_id.return_value = entry
        service.repository.update.return_value = make_setting(
            setting_id=entry.id, setting_value="smtp2.example.com"
        )

        payload = SettingsUpdate(setting_value="smtp2.example.com", updated_by=1)
        result = await service.update_setting(entry.id, payload)

        assert result.setting_value == "smtp2.example.com"

    async def test_update_nonexistent_raises_not_found(self, service):
        service.repository.get_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.update_setting(uuid.uuid4(), SettingsUpdate(description="ghost"))

    async def test_update_non_editable_entry_raises_business_rule_exception(self, service):
        entry = make_setting(is_editable=False)
        service.repository.get_by_id.return_value = entry

        with pytest.raises(BusinessRuleException):
            await service.update_setting(entry.id, SettingsUpdate(description="new"))
        service.repository.update.assert_not_awaited()

    async def test_update_resulting_encrypted_and_public_rejected(self, service):
        entry = make_setting(is_encrypted=False, is_public=False)
        service.repository.get_by_id.return_value = entry

        payload = SettingsUpdate(is_encrypted=True, is_public=True)
        with pytest.raises(BusinessRuleException):
            await service.update_setting(entry.id, payload)

    async def test_update_value_against_existing_data_type_validated(self, service):
        entry = make_setting(data_type=SettingDataType.INTEGER, setting_value=587)
        service.repository.get_by_id.return_value = entry

        payload = SettingsUpdate(setting_value="not-an-integer")
        with pytest.raises(ValidationException):
            await service.update_setting(entry.id, payload)

    async def test_update_with_no_fields_is_a_noop(self, service):
        entry = make_setting()
        service.repository.get_by_id.return_value = entry

        result = await service.update_setting(entry.id, SettingsUpdate())

        service.repository.update.assert_not_awaited()
        assert result.id == entry.id

    async def test_update_data_type_changes_effective_type_for_validation(self, service):
        entry = make_setting(data_type=SettingDataType.STRING, setting_value="30")
        service.repository.get_by_id.return_value = entry
        service.repository.update.return_value = make_setting(
            setting_id=entry.id, data_type=SettingDataType.INTEGER, setting_value=30
        )

        payload = SettingsUpdate(data_type=SettingDataType.INTEGER, setting_value=30)
        result = await service.update_setting(entry.id, payload)
        assert result.data_type == SettingDataType.INTEGER


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #

class TestDeleteSetting:

    async def test_delete_setting_success(self, service):
        entry = make_setting()
        service.repository.get_by_id.return_value = entry

        await service.delete_setting(entry.id)
        service.repository.delete.assert_awaited_once_with(entry)

    async def test_delete_nonexistent_raises_not_found(self, service):
        service.repository.get_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.delete_setting(uuid.uuid4())

    @pytest.mark.parametrize(
        "category,setting_key",
        [
            (SettingCategory.SYSTEM, "SYSTEM_MAINTENANCE_MODE"),
            (SettingCategory.SYSTEM, "SYSTEM_VERSION"),
            (SettingCategory.SECURITY, "SECURITY_PASSWORD_POLICY"),
            (SettingCategory.SECURITY, "SECURITY_SESSION_TIMEOUT_MINUTES"),
        ],
    )
    async def test_delete_protected_system_setting_raises_business_rule_exception(
        self, service, category, setting_key
    ):
        entry = make_setting(category=category, setting_key=setting_key)
        service.repository.get_by_id.return_value = entry

        with pytest.raises(BusinessRuleException):
            await service.delete_setting(entry.id)
        service.repository.delete.assert_not_awaited()

    async def test_delete_non_protected_system_category_setting_succeeds(self, service):
        entry = make_setting(category=SettingCategory.SYSTEM, setting_key="SOME_OTHER_KEY")
        service.repository.get_by_id.return_value = entry

        await service.delete_setting(entry.id)
        service.repository.delete.assert_awaited_once()


# --------------------------------------------------------------------------- #
# List / Search
# --------------------------------------------------------------------------- #

class TestListAndSearchSettings:

    async def test_list_settings_returns_items_and_pagination(self, service):
        entries = [make_setting(setting_key=f"KEY_{i}") for i in range(3)]
        service.repository.list_settings.return_value = (entries, 3)

        filters = SettingsFilter(page=1, page_size=20)
        result = await service.list_settings(filters)

        assert result.total == 3
        assert len(result.items) == 3
        assert result.total_pages == 1

    async def test_list_settings_computes_total_pages_correctly(self, service):
        entries = [make_setting(setting_key=f"KEY_{i}") for i in range(5)]
        service.repository.list_settings.return_value = (entries, 42)

        filters = SettingsFilter(page=1, page_size=5)
        result = await service.list_settings(filters)

        assert result.total_pages == 9  # ceil(42 / 5)

    async def test_search_settings_success(self, service):
        entries = [make_setting()]
        service.repository.search_settings.return_value = (entries, 1)

        result = await service.search_settings("SMTP")
        assert result.total == 1

    async def test_search_settings_blank_term_raises_validation_exception(self, service):
        with pytest.raises(ValidationException):
            await service.search_settings("   ")

    async def test_search_settings_no_match_returns_empty(self, service):
        service.repository.search_settings.return_value = ([], 0)
        result = await service.search_settings("no-match-xyz")
        assert result.items == []
        assert result.total == 0


# --------------------------------------------------------------------------- #
# Scoped lookups
# --------------------------------------------------------------------------- #

class TestScopedLookups:

    async def test_get_public_settings_returns_only_public(self, service):
        entries = [make_setting(is_public=True) for _ in range(2)]
        service.repository.get_public_settings.return_value = entries

        results = await service.get_public_settings()
        assert len(results) == 2
        assert all(item.is_public for item in results)

    async def test_get_editable_settings_returns_only_editable(self, service):
        entries = [make_setting(is_editable=True) for _ in range(2)]
        service.repository.get_editable_settings.return_value = entries

        results = await service.get_editable_settings()
        assert all(item.is_editable for item in results)

    async def test_get_encrypted_settings_returns_only_encrypted(self, service):
        entries = [make_setting(is_encrypted=True, is_public=False) for _ in range(2)]
        service.repository.get_encrypted_settings.return_value = entries

        results = await service.get_encrypted_settings()
        assert all(item.is_encrypted for item in results)


# --------------------------------------------------------------------------- #
# Bulk operations
# --------------------------------------------------------------------------- #

class TestBulkUpdateSettings:

    async def test_bulk_update_success(self, service):
        entry_1, entry_2 = make_setting(), make_setting(setting_key="SMTP_PORT")
        service.repository.get_by_id.side_effect = [entry_1, entry_2]
        service.repository.bulk_update.return_value = [entry_1, entry_2]

        updates = [
            (entry_1.id, SettingsUpdate(description="Updated one.")),
            (entry_2.id, SettingsUpdate(description="Updated two.")),
        ]
        results = await service.bulk_update_settings(updates)
        assert len(results) == 2

    async def test_bulk_update_empty_raises_validation_exception(self, service):
        with pytest.raises(ValidationException):
            await service.bulk_update_settings([])

    async def test_bulk_update_exceeds_max_batch_size_raises_validation_exception(self, service):
        oversized = [
            (uuid.uuid4(), SettingsUpdate(description="x"))
            for _ in range(service.MAX_BULK_UPDATE_SIZE + 1)
        ]
        with pytest.raises(ValidationException):
            await service.bulk_update_settings(oversized)

    async def test_bulk_update_nonexistent_id_raises_not_found(self, service):
        service.repository.get_by_id.return_value = None
        updates = [(uuid.uuid4(), SettingsUpdate(description="ghost"))]
        with pytest.raises(NotFoundException):
            await service.bulk_update_settings(updates)

    async def test_bulk_update_non_editable_entry_raises_business_rule_exception(self, service):
        entry = make_setting(is_editable=False)
        service.repository.get_by_id.return_value = entry

        updates = [(entry.id, SettingsUpdate(description="should fail"))]
        with pytest.raises(BusinessRuleException):
            await service.bulk_update_settings(updates)


class TestBulkDeleteSettings:

    async def test_bulk_delete_success(self, service):
        entry_1, entry_2 = make_setting(), make_setting(setting_key="SMTP_PORT")
        service.repository.get_by_id.side_effect = [entry_1, entry_2]
        service.repository.bulk_delete.return_value = 2

        deleted_count = await service.bulk_delete_settings([entry_1.id, entry_2.id])
        assert deleted_count == 2

    async def test_bulk_delete_empty_raises_validation_exception(self, service):
        with pytest.raises(ValidationException):
            await service.bulk_delete_settings([])

    async def test_bulk_delete_exceeds_max_batch_size_raises_validation_exception(self, service):
        oversized_ids = [uuid.uuid4() for _ in range(service.MAX_BULK_DELETE_SIZE + 1)]
        with pytest.raises(ValidationException):
            await service.bulk_delete_settings(oversized_ids)

    async def test_bulk_delete_nonexistent_id_raises_not_found(self, service):
        service.repository.get_by_id.return_value = None
        with pytest.raises(NotFoundException):
            await service.bulk_delete_settings([uuid.uuid4()])

    async def test_bulk_delete_protected_setting_raises_business_rule_exception(self, service):
        entry = make_setting(category=SettingCategory.SYSTEM, setting_key="SYSTEM_VERSION")
        service.repository.get_by_id.return_value = entry

        with pytest.raises(BusinessRuleException):
            await service.bulk_delete_settings([entry.id])
        service.repository.bulk_delete.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #

class TestGetStatistics:

    async def test_get_statistics_returns_aggregates(self, service):
        service.repository.get_total_count.return_value = 42
        service.repository.count_public.return_value = 10
        service.repository.count_editable.return_value = 38
        service.repository.count_encrypted.return_value = 2
        service.repository.count_by_category.return_value = {"EMAIL": 5, "SECURITY": 3}
        service.repository.count_by_data_type.return_value = {"STRING": 20, "INTEGER": 10}

        stats = await service.get_statistics()

        assert stats.total_settings == 42
        assert stats.public_count == 10
        assert stats.editable_count == 38
        assert stats.encrypted_count == 2
        assert stats.by_category == {"EMAIL": 5, "SECURITY": 3}

    async def test_get_statistics_date_from_after_date_to_raises_validation_exception(
        self, service
    ):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationException):
            await service.get_statistics(date_from=now, date_to=now - timedelta(days=1))


# --------------------------------------------------------------------------- #
# Cache snapshot
# --------------------------------------------------------------------------- #

class TestGetCacheSnapshot:

    async def test_cache_snapshot_redacts_encrypted_values(self, service):
        encrypted_entry = make_setting(
            category=SettingCategory.SYSTEM,
            setting_key="API_SECRET_TOKEN",
            setting_value="real-secret",
            data_type=SettingDataType.PASSWORD,
            is_encrypted=True,
        )
        service.repository.list_settings.return_value = ([encrypted_entry], 1)

        snapshot = await service.get_cache_snapshot()

        key = "SYSTEM.API_SECRET_TOKEN"
        assert snapshot[key]["value"] == "***REDACTED***"
        assert snapshot[key]["is_encrypted"] is True

    async def test_cache_snapshot_preserves_non_encrypted_values(self, service):
        entry = make_setting(setting_value="smtp.example.com")
        service.repository.list_settings.return_value = ([entry], 1)

        snapshot = await service.get_cache_snapshot()

        key = "EMAIL.SMTP_HOST"
        assert snapshot[key]["value"] == "smtp.example.com"
        assert snapshot[key]["is_encrypted"] is False

    async def test_cache_snapshot_scoped_to_category_uses_get_by_category(self, service):
        entry = make_setting()
        service.repository.get_by_category.return_value = [entry]

        snapshot = await service.get_cache_snapshot(category=SettingCategory.EMAIL)

        service.repository.get_by_category.assert_awaited_once()
        service.repository.list_settings.assert_not_awaited()
        assert "EMAIL.SMTP_HOST" in snapshot


# --------------------------------------------------------------------------- #
# Domain exception status codes
# --------------------------------------------------------------------------- #

class TestDomainExceptionStatusCodes:

    async def test_not_found_exception_status_code(self, service):
        service.repository.get_by_id.return_value = None
        with pytest.raises(NotFoundException) as exc_info:
            await service.get_setting(uuid.uuid4())
        assert exc_info.value.status_code == 404

    async def test_validation_exception_status_code(self, service, valid_create_payload):
        valid_create_payload.setting_key = "1_BAD_KEY"
        with pytest.raises(ValidationException) as exc_info:
            await service.create_setting(valid_create_payload)
        assert exc_info.value.status_code == 400

    async def test_duplicate_resource_exception_status_code(self, service, valid_create_payload):
        service.repository.exists_by_category_and_key.return_value = True
        with pytest.raises(DuplicateResourceException) as exc_info:
            await service.create_setting(valid_create_payload)
        assert exc_info.value.status_code == 409

    async def test_business_rule_exception_status_code(self, service):
        entry = make_setting(is_editable=False)
        service.repository.get_by_id.return_value = entry
        with pytest.raises(BusinessRuleException) as exc_info:
            await service.update_setting(entry.id, SettingsUpdate(description="x"))
        assert exc_info.value.status_code == 422