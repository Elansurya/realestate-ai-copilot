"""Business/service layer for the Settings module.

The :class:`SettingsService` owns all domain validation and business
rules for system configuration entries. It orchestrates the
:class:`~app.repositories.settings_repository.SettingsRepository` for
persistence and never performs raw SQL or ORM queries itself. Only
domain exceptions from :mod:`app.core.exceptions` are raised from this
layer; HTTP concerns are the responsibility of the router layer.
"""

import re
import uuid
from datetime import datetime
from typing import Any, Optional, Sequence

from app.core.exceptions import (
    BusinessRuleException,
    DuplicateResourceException,
    NotFoundException,
    ValidationException,
)
from app.models.settings import SettingCategory, SettingDataType, Settings
from app.repositories.settings_repository import SettingsRepository
from app.schemas.settings import (
    SettingsCreate,
    SettingsFilter,
    SettingsListResponse,
    SettingsResponse,
    SettingsStatisticsResponse,
    SettingsUpdate,
)

__all__ = ["SettingsService"]


class SettingsService:
    """Encapsulates business rules and orchestration for setting entries.

    Attributes:
        repository: Data-access layer used for all persistence operations.
    """

    #: Maximum length permitted for a `setting_key`.
    MAX_KEY_LENGTH: int = 150

    #: Pattern a `setting_key` must match: uppercase letters, digits,
    #: underscores, and dots, starting with a letter.
    KEY_PATTERN: re.Pattern[str] = re.compile(r"^[A-Z][A-Z0-9_.]*$")

    #: Maximum number of entries accepted in a single bulk-update call.
    MAX_BULK_UPDATE_SIZE: int = 200

    #: Maximum number of ids accepted in a single bulk-delete call.
    MAX_BULK_DELETE_SIZE: int = 200

    #: (category, setting_key) pairs that are protected, core system
    #: settings. These may be read and their values changed like any
    #: other setting, but can never be deleted and can never have their
    #: `is_editable` flag turned off-then-bypassed -- i.e. they can never
    #: be deleted or have their identity (category/key) altered.
    PROTECTED_SYSTEM_KEYS: frozenset[tuple[SettingCategory, str]] = frozenset(
        {
            (SettingCategory.SYSTEM, "SYSTEM_MAINTENANCE_MODE"),
            (SettingCategory.SYSTEM, "SYSTEM_VERSION"),
            (SettingCategory.SECURITY, "SECURITY_PASSWORD_POLICY"),
            (SettingCategory.SECURITY, "SECURITY_SESSION_TIMEOUT_MINUTES"),
        }
    )

    def __init__(self, repository: SettingsRepository) -> None:
        """Initializes the service with its repository dependency.

        Args:
            repository: The settings repository used for persistence.
        """
        self.repository = repository

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_category(category: SettingCategory) -> SettingCategory:
        """Validates that the supplied category is a recognized enum member.

        Args:
            category: The category to validate.

        Returns:
            SettingCategory: The validated category.

        Raises:
            ValidationException: If the category is not a member of
                :class:`SettingCategory`.
        """
        try:
            return SettingCategory(category)
        except ValueError as exc:
            raise ValidationException(f"Invalid setting category: {category!r}.") from exc

    @staticmethod
    def _validate_data_type(data_type: SettingDataType) -> SettingDataType:
        """Validates that the supplied data type is a recognized enum member.

        Args:
            data_type: The data type to validate.

        Returns:
            SettingDataType: The validated data type.

        Raises:
            ValidationException: If the data type is not a member of
                :class:`SettingDataType`.
        """
        try:
            return SettingDataType(data_type)
        except ValueError as exc:
            raise ValidationException(f"Invalid setting data type: {data_type!r}.") from exc

    @classmethod
    def _validate_setting_key(cls, setting_key: str) -> str:
        """Validates that a setting key is present and well-formed.

        Args:
            setting_key: The raw setting key.

        Returns:
            str: The trimmed, normalized (uppercased) setting key.

        Raises:
            ValidationException: If the key is empty, too long, or does
                not match the required naming pattern.
        """
        if not setting_key or not setting_key.strip():
            raise ValidationException("setting_key must not be empty.")

        normalized = setting_key.strip().upper()

        if len(normalized) > cls.MAX_KEY_LENGTH:
            raise ValidationException(
                f"setting_key must not exceed {cls.MAX_KEY_LENGTH} characters."
            )
        if not cls.KEY_PATTERN.match(normalized):
            raise ValidationException(
                "setting_key must start with a letter and contain only "
                "uppercase letters, digits, underscores, and dots."
            )
        return normalized

    @staticmethod
    def _validate_value_against_data_type(
        setting_value: Any, data_type: SettingDataType
    ) -> Any:
        """Validates that a value's shape is consistent with its declared data type.

        Args:
            setting_value: The raw value to validate.
            data_type: The declared logical data type of the value.

        Returns:
            Any: The validated value, unchanged.

        Raises:
            ValidationException: If the value's Python type does not
                match what is expected for the declared data type.
        """
        if setting_value is None:
            return setting_value

        if data_type == SettingDataType.STRING:
            if not isinstance(setting_value, str):
                raise ValidationException("setting_value must be a string for data_type STRING.")
        elif data_type == SettingDataType.INTEGER:
            if isinstance(setting_value, bool) or not isinstance(setting_value, int):
                raise ValidationException("setting_value must be an integer for data_type INTEGER.")
        elif data_type == SettingDataType.FLOAT:
            if isinstance(setting_value, bool) or not isinstance(setting_value, (int, float)):
                raise ValidationException("setting_value must be a number for data_type FLOAT.")
        elif data_type == SettingDataType.BOOLEAN:
            if not isinstance(setting_value, bool):
                raise ValidationException("setting_value must be a boolean for data_type BOOLEAN.")
        elif data_type == SettingDataType.JSON:
            if not isinstance(setting_value, (dict, list)):
                raise ValidationException(
                    "setting_value must be a JSON object or array for data_type JSON."
                )
        elif data_type == SettingDataType.ARRAY:
            if not isinstance(setting_value, list):
                raise ValidationException("setting_value must be an array for data_type ARRAY.")
        elif data_type == SettingDataType.DATE:
            if not isinstance(setting_value, str):
                raise ValidationException(
                    "setting_value must be an ISO-8601 date string for data_type DATE."
                )
            try:
                datetime.fromisoformat(setting_value)
            except ValueError as exc:
                raise ValidationException(
                    "setting_value is not a valid ISO-8601 date for data_type DATE."
                ) from exc
        elif data_type == SettingDataType.DATETIME:
            if not isinstance(setting_value, str):
                raise ValidationException(
                    "setting_value must be an ISO-8601 datetime string for data_type DATETIME."
                )
            try:
                datetime.fromisoformat(setting_value)
            except ValueError as exc:
                raise ValidationException(
                    "setting_value is not a valid ISO-8601 datetime for data_type DATETIME."
                ) from exc
        elif data_type == SettingDataType.EMAIL:
            if not isinstance(setting_value, str) or "@" not in setting_value:
                raise ValidationException(
                    "setting_value must be a valid email string for data_type EMAIL."
                )
        elif data_type == SettingDataType.URL:
            if not isinstance(setting_value, str) or not re.match(
                r"^https?://", setting_value
            ):
                raise ValidationException(
                    "setting_value must be a valid http(s) URL for data_type URL."
                )
        elif data_type == SettingDataType.PASSWORD:
            if not isinstance(setting_value, str) or not setting_value:
                raise ValidationException(
                    "setting_value must be a non-empty string for data_type PASSWORD."
                )

        return setting_value

    @staticmethod
    def _validate_against_rules(
        setting_value: Any, validation_rules: Optional[dict[str, Any]]
    ) -> None:
        """Validates a value against an optional caller-supplied ruleset.

        Supported rule keys: ``min``, ``max`` (numeric bounds),
        ``min_length``, ``max_length`` (string/array length bounds),
        ``pattern`` (regex a string must fully match), and ``enum`` (an
        allow-list of permitted values).

        Args:
            setting_value: The value to validate.
            validation_rules: The ruleset to validate against, if any.

        Raises:
            ValidationException: If the value violates any supplied rule.
        """
        if not validation_rules or setting_value is None:
            return

        if "enum" in validation_rules:
            allowed = validation_rules["enum"]
            if setting_value not in allowed:
                raise ValidationException(
                    f"setting_value must be one of: {allowed}."
                )

        if isinstance(setting_value, (int, float)) and not isinstance(setting_value, bool):
            if "min" in validation_rules and setting_value < validation_rules["min"]:
                raise ValidationException(
                    f"setting_value must be >= {validation_rules['min']}."
                )
            if "max" in validation_rules and setting_value > validation_rules["max"]:
                raise ValidationException(
                    f"setting_value must be <= {validation_rules['max']}."
                )

        if isinstance(setting_value, (str, list)):
            length = len(setting_value)
            if "min_length" in validation_rules and length < validation_rules["min_length"]:
                raise ValidationException(
                    f"setting_value must have length >= {validation_rules['min_length']}."
                )
            if "max_length" in validation_rules and length > validation_rules["max_length"]:
                raise ValidationException(
                    f"setting_value must have length <= {validation_rules['max_length']}."
                )

        if "pattern" in validation_rules and isinstance(setting_value, str):
            if not re.fullmatch(validation_rules["pattern"], setting_value):
                raise ValidationException(
                    "setting_value does not match the required pattern."
                )

    @staticmethod
    def _validate_encryption_flags(is_encrypted: bool, is_public: bool) -> None:
        """Ensures an encrypted setting is never simultaneously public.

        Args:
            is_encrypted: Whether the setting's value is encrypted.
            is_public: Whether the setting is exposed to unauthenticated
                clients.

        Raises:
            BusinessRuleException: If both flags are true at once.
        """
        if is_encrypted and is_public:
            raise BusinessRuleException(
                "A setting cannot be both is_encrypted and is_public."
            )

    @classmethod
    def _is_protected_system_setting(
        cls, category: SettingCategory, setting_key: str
    ) -> bool:
        """Checks whether a (category, key) pair is a protected system setting.

        Args:
            category: The setting's category.
            setting_key: The setting's key.

        Returns:
            bool: ``True`` if the pair is protected against deletion and
            identity changes.
        """
        return (category, setting_key) in cls.PROTECTED_SYSTEM_KEYS

    def _guard_editable(self, entry: Settings) -> None:
        """Ensures a setting is currently editable before mutating it.

        Args:
            entry: The setting entry about to be modified.

        Raises:
            BusinessRuleException: If ``entry.is_editable`` is ``False``.
        """
        if not entry.is_editable:
            raise BusinessRuleException(
                f"Setting '{entry.category.value}.{entry.setting_key}' is not editable."
            )

    def _guard_not_protected(self, entry: Settings, *, action: str) -> None:
        """Ensures an operation is not being performed against a protected setting.

        Args:
            entry: The setting entry targeted by the operation.
            action: A short description of the attempted action, used in
                the raised exception's message (e.g. ``"deleted"``).

        Raises:
            BusinessRuleException: If the entry is a protected system
                setting.
        """
        if self._is_protected_system_setting(entry.category, entry.setting_key):
            raise BusinessRuleException(
                f"System setting '{entry.category.value}.{entry.setting_key}' "
                f"cannot be {action}."
            )

    async def _validate_and_normalize_create(
        self, payload: SettingsCreate
    ) -> dict[str, Any]:
        """Runs full validation on a creation payload and returns ORM-ready data.

        Args:
            payload: The incoming setting creation schema.

        Returns:
            dict[str, Any]: A mapping of column names to validated values,
            ready to be passed to the repository's ``create``.

        Raises:
            ValidationException: If any field fails validation.
            BusinessRuleException: If the encryption/public flag
                combination is invalid.
            DuplicateResourceException: If a setting with the same
                (category, setting_key) pair already exists.
        """
        category = self._validate_category(payload.category)
        data_type = self._validate_data_type(payload.data_type)
        setting_key = self._validate_setting_key(payload.setting_key)
        self._validate_encryption_flags(payload.is_encrypted, payload.is_public)
        self._validate_value_against_data_type(payload.setting_value, data_type)
        self._validate_against_rules(payload.setting_value, payload.validation_rules)

        if await self.repository.exists_by_category_and_key(category, setting_key):
            raise DuplicateResourceException(
                f"A setting already exists for category '{category.value}' "
                f"and key '{setting_key}'."
            )

        return {
            "category": category,
            "setting_key": setting_key,
            "setting_value": payload.setting_value,
            "description": payload.description,
            "data_type": data_type,
            "is_public": payload.is_public,
            "is_editable": payload.is_editable,
            "is_encrypted": payload.is_encrypted,
            "validation_rules": payload.validation_rules,
            "created_by": payload.created_by,
            "updated_by": payload.created_by,
        }

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    async def create_setting(self, payload: SettingsCreate) -> SettingsResponse:
        """Validates and persists a single setting entry.

        Args:
            payload: The setting creation request.

        Returns:
            SettingsResponse: The persisted setting entry.

        Raises:
            ValidationException: If any field fails validation.
            BusinessRuleException: If the encryption/public flag
                combination is invalid.
            DuplicateResourceException: If a setting with the same
                (category, setting_key) pair already exists.
        """
        data = await self._validate_and_normalize_create(payload)
        entry = await self.repository.create(data)
        return SettingsResponse.model_validate(entry)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def get_setting(self, setting_id: uuid.UUID) -> SettingsResponse:
        """Retrieves a single setting entry by id.

        Args:
            setting_id: The UUID primary key of the entry.

        Returns:
            SettingsResponse: The matching setting entry.

        Raises:
            NotFoundException: If no entry with the given id exists.
        """
        entry = await self._get_entry_or_raise(setting_id)
        return SettingsResponse.model_validate(entry)

    async def get_setting_by_key(self, setting_key: str) -> SettingsResponse:
        """Retrieves a single setting entry by its key.

        Args:
            setting_key: The configuration key to look up.

        Returns:
            SettingsResponse: The matching setting entry.

        Raises:
            ValidationException: If ``setting_key`` is empty.
            NotFoundException: If no entry with the given key exists.
        """
        if not setting_key or not setting_key.strip():
            raise ValidationException("setting_key must not be empty.")

        entry = await self.repository.get_by_key(setting_key.strip().upper())
        if entry is None:
            raise NotFoundException(f"Setting with key '{setting_key}' was not found.")
        return SettingsResponse.model_validate(entry)

    async def get_setting_by_category_and_key(
        self, category: SettingCategory, setting_key: str
    ) -> SettingsResponse:
        """Retrieves a single setting entry by its (category, key) pair.

        Args:
            category: The functional category the setting belongs to.
            setting_key: The configuration key within that category.

        Returns:
            SettingsResponse: The matching setting entry.

        Raises:
            ValidationException: If ``category`` or ``setting_key`` is
                invalid.
            NotFoundException: If no matching entry exists.
        """
        validated_category = self._validate_category(category)
        if not setting_key or not setting_key.strip():
            raise ValidationException("setting_key must not be empty.")

        entry = await self.repository.get_by_category_and_key(
            validated_category, setting_key.strip().upper()
        )
        if entry is None:
            raise NotFoundException(
                f"Setting '{validated_category.value}.{setting_key}' was not found."
            )
        return SettingsResponse.model_validate(entry)

    async def get_settings_by_category(
        self, category: SettingCategory
    ) -> list[SettingsResponse]:
        """Retrieves every setting entry within a given category.

        Args:
            category: The functional category to filter by.

        Returns:
            list[SettingsResponse]: All entries in the category.

        Raises:
            ValidationException: If ``category`` is invalid.
        """
        validated_category = self._validate_category(category)
        entries = await self.repository.get_by_category(validated_category)
        return [SettingsResponse.model_validate(entry) for entry in entries]

    async def _get_entry_or_raise(self, setting_id: uuid.UUID) -> Settings:
        """Fetches a setting ORM entry by id or raises if it does not exist.

        Args:
            setting_id: The UUID primary key of the entry.

        Returns:
            Settings: The matching ORM entry.

        Raises:
            NotFoundException: If no entry with the given id exists.
        """
        entry = await self.repository.get_by_id(setting_id)
        if entry is None:
            raise NotFoundException(f"Setting with id {setting_id} was not found.")
        return entry

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update_setting(
        self, setting_id: uuid.UUID, payload: SettingsUpdate
    ) -> SettingsResponse:
        """Validates and applies a partial update to a setting entry.

        Args:
            setting_id: The UUID primary key of the entry to update.
            payload: The partial update request.

        Returns:
            SettingsResponse: The updated setting entry.

        Raises:
            NotFoundException: If no entry with the given id exists.
            BusinessRuleException: If the entry is not editable, or if
                the resulting encryption/public flag combination is
                invalid.
            ValidationException: If the new value/data_type/validation
                rules combination fails validation.
        """
        entry = await self._get_entry_or_raise(setting_id)
        self._guard_editable(entry)

        update_data: dict[str, Any] = {}

        effective_data_type = entry.data_type
        if payload.data_type is not None:
            effective_data_type = self._validate_data_type(payload.data_type)
            update_data["data_type"] = effective_data_type

        effective_is_public = (
            payload.is_public if payload.is_public is not None else entry.is_public
        )
        effective_is_encrypted = (
            payload.is_encrypted
            if payload.is_encrypted is not None
            else entry.is_encrypted
        )
        self._validate_encryption_flags(effective_is_encrypted, effective_is_public)

        effective_value = (
            payload.setting_value
            if payload.setting_value is not None
            else entry.setting_value
        )
        effective_rules = (
            payload.validation_rules
            if payload.validation_rules is not None
            else entry.validation_rules
        )
        if payload.setting_value is not None or payload.data_type is not None:
            self._validate_value_against_data_type(effective_value, effective_data_type)
        if payload.setting_value is not None:
            self._validate_against_rules(effective_value, effective_rules)
            update_data["setting_value"] = effective_value

        if payload.description is not None:
            update_data["description"] = payload.description
        if payload.is_public is not None:
            update_data["is_public"] = payload.is_public
        if payload.is_editable is not None:
            update_data["is_editable"] = payload.is_editable
        if payload.is_encrypted is not None:
            update_data["is_encrypted"] = payload.is_encrypted
        if payload.validation_rules is not None:
            update_data["validation_rules"] = payload.validation_rules
        if payload.updated_by is not None:
            update_data["updated_by"] = payload.updated_by

        if not update_data:
            return SettingsResponse.model_validate(entry)

        updated_entry = await self.repository.update(entry, update_data)
        return SettingsResponse.model_validate(updated_entry)

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    async def delete_setting(self, setting_id: uuid.UUID) -> None:
        """Validates and deletes a single setting entry.

        Args:
            setting_id: The UUID primary key of the entry to delete.

        Raises:
            NotFoundException: If no entry with the given id exists.
            BusinessRuleException: If the entry is a protected system
                setting.
        """
        entry = await self._get_entry_or_raise(setting_id)
        self._guard_not_protected(entry, action="deleted")
        await self.repository.delete(entry)

    # ------------------------------------------------------------------
    # Listing / searching
    # ------------------------------------------------------------------

    async def list_settings(self, filters: SettingsFilter) -> SettingsListResponse:
        """Retrieves a filtered, sorted, paginated page of setting entries.

        Args:
            filters: The combined filter, sort, and pagination parameters.

        Returns:
            SettingsListResponse: The requested page of entries plus
            pagination metadata.
        """
        items, total = await self.repository.list_settings(
            category=filters.category,
            setting_key=filters.setting_key,
            data_type=filters.data_type,
            is_public=filters.is_public,
            is_editable=filters.is_editable,
            is_encrypted=filters.is_encrypted,
            search=filters.search,
            date_from=filters.date_from,
            date_to=filters.date_to,
            page=filters.page,
            page_size=filters.page_size,
            sort_by=filters.sort_by,
            sort_order=filters.sort_order,
        )
        total_pages = (
            (total + filters.page_size - 1) // filters.page_size
            if filters.page_size
            else 0
        )
        return SettingsListResponse(
            items=[SettingsResponse.model_validate(item) for item in items],
            total=total,
            page=filters.page,
            page_size=filters.page_size,
            total_pages=total_pages,
        )

    async def search_settings(
        self,
        search_term: str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> SettingsListResponse:
        """Performs a validated free-text search over setting keys/descriptions.

        Args:
            search_term: The text to search for.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            SettingsListResponse: The matching page of entries plus
            pagination metadata.

        Raises:
            ValidationException: If the search term is empty.
        """
        if not search_term or not search_term.strip():
            raise ValidationException("Search term must not be empty.")

        items, total = await self.repository.search_settings(
            search_term.strip(),
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        total_pages = (total + page_size - 1) // page_size if page_size else 0
        return SettingsListResponse(
            items=[SettingsResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    # ------------------------------------------------------------------
    # Scoped convenience lookups
    # ------------------------------------------------------------------

    async def get_public_settings(self) -> list[SettingsResponse]:
        """Retrieves every setting entry flagged as publicly exposable.

        Returns:
            list[SettingsResponse]: All entries with ``is_public`` true.
        """
        entries = await self.repository.get_public_settings()
        return [SettingsResponse.model_validate(entry) for entry in entries]

    async def get_editable_settings(self) -> list[SettingsResponse]:
        """Retrieves every setting entry flagged as editable.

        Returns:
            list[SettingsResponse]: All entries with ``is_editable`` true.
        """
        entries = await self.repository.get_editable_settings()
        return [SettingsResponse.model_validate(entry) for entry in entries]

    async def get_encrypted_settings(self) -> list[SettingsResponse]:
        """Retrieves every setting entry flagged as encrypted.

        Returns:
            list[SettingsResponse]: All entries with ``is_encrypted`` true.
        """
        entries = await self.repository.get_encrypted_settings()
        return [SettingsResponse.model_validate(entry) for entry in entries]

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    async def bulk_update_settings(
        self, updates: Sequence[tuple[uuid.UUID, SettingsUpdate]]
    ) -> list[SettingsResponse]:
        """Validates and applies a batch of partial updates to setting entries.

        Args:
            updates: Sequence of ``(setting_id, payload)`` pairs.

        Returns:
            list[SettingsResponse]: The updated entries, in the same
            relative order as the resolvable input ids.

        Raises:
            ValidationException: If the batch is empty or exceeds the
                maximum allowed size.
            NotFoundException: If any referenced setting does not exist.
            BusinessRuleException: If any referenced entry is not
                editable, or a resulting flag combination is invalid.
        """
        if not updates:
            raise ValidationException("At least one update must be supplied for bulk update.")
        if len(updates) > self.MAX_BULK_UPDATE_SIZE:
            raise ValidationException(
                "Bulk update exceeds the maximum batch size of "
                f"{self.MAX_BULK_UPDATE_SIZE}."
            )

        prepared: list[tuple[uuid.UUID, dict[str, Any]]] = []
        for setting_id, payload in updates:
            entry = await self._get_entry_or_raise(setting_id)
            self._guard_editable(entry)

            effective_data_type = (
                self._validate_data_type(payload.data_type)
                if payload.data_type is not None
                else entry.data_type
            )
            effective_is_public = (
                payload.is_public if payload.is_public is not None else entry.is_public
            )
            effective_is_encrypted = (
                payload.is_encrypted
                if payload.is_encrypted is not None
                else entry.is_encrypted
            )
            self._validate_encryption_flags(effective_is_encrypted, effective_is_public)

            if payload.setting_value is not None:
                effective_rules = (
                    payload.validation_rules
                    if payload.validation_rules is not None
                    else entry.validation_rules
                )
                self._validate_value_against_data_type(
                    payload.setting_value, effective_data_type
                )
                self._validate_against_rules(payload.setting_value, effective_rules)

            row_data: dict[str, Any] = {}
            if payload.setting_value is not None:
                row_data["setting_value"] = payload.setting_value
            if payload.description is not None:
                row_data["description"] = payload.description
            if payload.data_type is not None:
                row_data["data_type"] = effective_data_type
            if payload.is_public is not None:
                row_data["is_public"] = payload.is_public
            if payload.is_editable is not None:
                row_data["is_editable"] = payload.is_editable
            if payload.is_encrypted is not None:
                row_data["is_encrypted"] = payload.is_encrypted
            if payload.validation_rules is not None:
                row_data["validation_rules"] = payload.validation_rules
            if payload.updated_by is not None:
                row_data["updated_by"] = payload.updated_by

            if row_data:
                prepared.append((setting_id, row_data))

        updated_entries = await self.repository.bulk_update(prepared)
        return [SettingsResponse.model_validate(entry) for entry in updated_entries]

    async def bulk_delete_settings(self, ids: Sequence[uuid.UUID]) -> int:
        """Validates and deletes a specific, bounded set of setting entries.

        Args:
            ids: The primary keys of the entries to delete.

        Returns:
            int: The number of entries deleted.

        Raises:
            ValidationException: If ``ids`` is empty or exceeds the
                maximum allowed batch size.
            NotFoundException: If any referenced setting does not exist.
            BusinessRuleException: If any referenced entry is a protected
                system setting.
        """
        if not ids:
            raise ValidationException("At least one id must be supplied for bulk delete.")
        if len(ids) > self.MAX_BULK_DELETE_SIZE:
            raise ValidationException(
                "Bulk delete exceeds the maximum batch size of "
                f"{self.MAX_BULK_DELETE_SIZE}."
            )

        deletable_ids: list[uuid.UUID] = []
        for setting_id in ids:
            entry = await self._get_entry_or_raise(setting_id)
            self._guard_not_protected(entry, action="deleted")
            deletable_ids.append(entry.id)

        return await self.repository.bulk_delete(deletable_ids)

    # ------------------------------------------------------------------
    # Dashboard / statistics
    # ------------------------------------------------------------------

    async def get_statistics(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> SettingsStatisticsResponse:
        """Computes aggregate setting statistics over an optional date range.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            SettingsStatisticsResponse: The computed aggregate statistics.

        Raises:
            ValidationException: If ``date_from`` is after ``date_to``.
        """
        if date_from and date_to and date_from > date_to:
            raise ValidationException("date_from must not be after date_to.")

        total = await self.repository.get_total_count(
            date_from=date_from, date_to=date_to
        )
        public_count = await self.repository.count_public(
            date_from=date_from, date_to=date_to
        )
        editable_count = await self.repository.count_editable(
            date_from=date_from, date_to=date_to
        )
        encrypted_count = await self.repository.count_encrypted(
            date_from=date_from, date_to=date_to
        )
        by_category = await self.repository.count_by_category(
            date_from=date_from, date_to=date_to
        )
        by_data_type = await self.repository.count_by_data_type(
            date_from=date_from, date_to=date_to
        )

        return SettingsStatisticsResponse(
            total_settings=total,
            public_count=public_count,
            editable_count=editable_count,
            encrypted_count=encrypted_count,
            by_category=by_category,
            by_data_type=by_data_type,
            date_from=date_from,
            date_to=date_to,
        )

    # ------------------------------------------------------------------
    # Cache refresh support
    # ------------------------------------------------------------------

    async def get_cache_snapshot(
        self, *, category: Optional[SettingCategory] = None
    ) -> dict[str, dict[str, Any]]:
        """Builds a flat, cache-ready snapshot of current setting values.

        Intended to be invoked by a caching layer (e.g. an in-memory or
        Redis-backed settings cache) after any create/update/delete
        operation, or on a scheduled refresh interval, to rehydrate its
        view of current configuration without exposing internal/encrypted
        values to unintended consumers.

        Args:
            category: Optional category to scope the snapshot to. If
                omitted, every category is included.

        Returns:
            dict[str, dict[str, Any]]: Mapping of ``"{category}.{setting_key}"``
            to a small dict of ``{"value": ..., "data_type": ..., "is_encrypted": ...}``.
            Encrypted values are represented by a redacted placeholder
            rather than their actual stored value, since this snapshot is
            intended for cache/consumer layers that should not handle
            secrets directly.
        """
        if category is not None:
            entries = await self.repository.get_by_category(
                self._validate_category(category)
            )
        else:
            entries, _ = await self.repository.list_settings(
                page=1, page_size=10_000, sort_by="category", sort_order="asc"
            )

        snapshot: dict[str, dict[str, Any]] = {}
        for entry in entries:
            cache_key = f"{entry.category.value}.{entry.setting_key}"
            snapshot[cache_key] = {
                "value": "***REDACTED***" if entry.is_encrypted else entry.setting_value,
                "data_type": entry.data_type.value,
                "is_encrypted": entry.is_encrypted,
            }
        return snapshot