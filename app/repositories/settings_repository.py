"""Data access layer for the Settings module.

This repository is intentionally free of business logic and domain
validation. It is responsible solely for translating well-formed
requests into SQLAlchemy 2.x async queries against the ``settings``
table and returning ORM instances or primitive aggregation results.
"""

import uuid
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy import Select, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import SettingCategory, SettingDataType, Settings

__all__ = ["SettingsRepository"]


class SettingsRepository:
    """Provides raw persistence operations for :class:`Settings` entities.

    Attributes:
        session: The active asynchronous SQLAlchemy session used for all
            database operations issued by this repository.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initializes the repository with an active database session.

        Args:
            session: The asynchronous SQLAlchemy session to use for queries.
        """
        self.session = session

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create(self, data: dict[str, Any]) -> Settings:
        """Persists a new setting entry.

        Args:
            data: Mapping of column names to values for the new row.

        Returns:
            Settings: The newly created, refreshed ORM instance.
        """
        entry = Settings(**data)
        self.session.add(entry)
        await self.session.flush()
        await self.session.refresh(entry)
        return entry

    async def update(self, entry: Settings, data: dict[str, Any]) -> Settings:
        """Applies a set of attribute updates to a tracked entry and persists it.

        Args:
            entry: A ``Settings`` instance retrieved from this session
                (e.g., via :meth:`get_by_id`) with updates to be applied.
            data: Mapping of attribute names to new values. Only keys that
                are actual columns on the ``Settings`` model are applied.

        Returns:
            Settings: The updated entry, refreshed with the latest
            database state (e.g., updated ``updated_at`` timestamp).
        """
        for field_name, value in data.items():
            if hasattr(entry, field_name):
                setattr(entry, field_name, value)

        self.session.add(entry)
        await self.session.flush()
        await self.session.refresh(entry)
        return entry

    async def delete(self, entry: Settings) -> None:
        """Deletes a single, already-tracked setting entry.

        Args:
            entry: The ``Settings`` instance to remove.
        """
        await self.session.delete(entry)
        await self.session.flush()

    async def get_by_id(self, setting_id: uuid.UUID) -> Optional[Settings]:
        """Fetches a single setting entry by its primary key.

        Args:
            setting_id: The UUID primary key of the entry.

        Returns:
            Optional[Settings]: The matching entry, or ``None`` if not found.
        """
        result = await self.session.execute(
            select(Settings).where(Settings.id == setting_id)
        )
        return result.scalar_one_or_none()

    async def get_by_key(self, setting_key: str) -> Optional[Settings]:
        """Fetches a single setting entry by its key, irrespective of category.

        Args:
            setting_key: The configuration key to look up.

        Returns:
            Optional[Settings]: The first matching entry, or ``None`` if
            no entry with this key exists. Intended for keys that are
            expected to be globally unique in practice; callers needing a
            category-scoped lookup should use
            :meth:`get_by_category_and_key` instead.
        """
        result = await self.session.execute(
            select(Settings).where(Settings.setting_key == setting_key)
        )
        return result.scalars().first()

    async def get_by_category_and_key(
        self, category: SettingCategory, setting_key: str
    ) -> Optional[Settings]:
        """Fetches a single setting entry by its (category, key) pair.

        Args:
            category: The functional category the setting belongs to.
            setting_key: The configuration key within that category.

        Returns:
            Optional[Settings]: The matching entry, or ``None`` if not found.
        """
        result = await self.session.execute(
            select(Settings).where(
                and_(
                    Settings.category == category,
                    Settings.setting_key == setting_key,
                )
            )
        )
        return result.scalar_one_or_none()

    async def get_by_category(self, category: SettingCategory) -> list[Settings]:
        """Fetches every setting entry within a given category.

        Args:
            category: The functional category to filter by.

        Returns:
            list[Settings]: All entries in the category, ordered by key.
        """
        result = await self.session.execute(
            select(Settings)
            .where(Settings.category == category)
            .order_by(Settings.setting_key.asc())
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Query building helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_filters(
        stmt: Select,
        *,
        category: Optional[SettingCategory] = None,
        setting_key: Optional[str] = None,
        data_type: Optional[SettingDataType] = None,
        is_public: Optional[bool] = None,
        is_editable: Optional[bool] = None,
        is_encrypted: Optional[bool] = None,
        search: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> Select:
        """Applies the supplied filter predicates to a base select statement.

        Args:
            stmt: The base SQLAlchemy select statement to constrain.
            category: Restrict to entries in this category.
            setting_key: Restrict to entries with this exact key.
            data_type: Restrict to entries with this data type.
            is_public: Restrict to entries with this public-exposure state.
            is_editable: Restrict to entries with this editability state.
            is_encrypted: Restrict to entries with this encryption state.
            search: Case-insensitive substring match against setting_key
                or description.
            date_from: Inclusive lower bound on ``created_at``.
            date_to: Inclusive upper bound on ``created_at``.

        Returns:
            Select: The statement with all applicable predicates applied.
        """
        conditions = []

        if category is not None:
            conditions.append(Settings.category == category)
        if setting_key is not None:
            conditions.append(Settings.setting_key == setting_key)
        if data_type is not None:
            conditions.append(Settings.data_type == data_type)
        if is_public is not None:
            conditions.append(Settings.is_public == is_public)
        if is_editable is not None:
            conditions.append(Settings.is_editable == is_editable)
        if is_encrypted is not None:
            conditions.append(Settings.is_encrypted == is_encrypted)
        if search:
            like_pattern = f"%{search}%"
            conditions.append(
                or_(
                    Settings.setting_key.ilike(like_pattern),
                    Settings.description.ilike(like_pattern),
                )
            )
        if date_from is not None:
            conditions.append(Settings.created_at >= date_from)
        if date_to is not None:
            conditions.append(Settings.created_at <= date_to)

        if conditions:
            stmt = stmt.where(and_(*conditions))
        return stmt

    # ------------------------------------------------------------------
    # Listing / searching
    # ------------------------------------------------------------------

    async def list_settings(
        self,
        *,
        category: Optional[SettingCategory] = None,
        setting_key: Optional[str] = None,
        data_type: Optional[SettingDataType] = None,
        is_public: Optional[bool] = None,
        is_editable: Optional[bool] = None,
        is_encrypted: Optional[bool] = None,
        search: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[Settings], int]:
        """Retrieves a filtered, sorted, paginated page of setting entries.

        Args:
            category: Optional category filter.
            setting_key: Optional exact-key filter.
            data_type: Optional data type filter.
            is_public: Optional public-exposure filter.
            is_editable: Optional editability filter.
            is_encrypted: Optional encryption filter.
            search: Optional free-text search on setting_key/description.
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            tuple[list[Settings], int]: The page of matching entries and
            the total count of entries matching the filters (ignoring
            pagination).
        """
        base_stmt = self._apply_filters(
            select(Settings),
            category=category,
            setting_key=setting_key,
            data_type=data_type,
            is_public=is_public,
            is_editable=is_editable,
            is_encrypted=is_encrypted,
            search=search,
            date_from=date_from,
            date_to=date_to,
        )

        count_stmt = self._apply_filters(
            select(func.count()).select_from(Settings),
            category=category,
            setting_key=setting_key,
            data_type=data_type,
            is_public=is_public,
            is_editable=is_editable,
            is_encrypted=is_encrypted,
            search=search,
            date_from=date_from,
            date_to=date_to,
        )

        sort_column = getattr(Settings, sort_by, Settings.created_at)
        order_expr = sort_column.asc() if sort_order == "asc" else sort_column.desc()

        page = max(page, 1)
        page_size = max(page_size, 1)
        offset = (page - 1) * page_size

        list_stmt = base_stmt.order_by(order_expr).offset(offset).limit(page_size)

        total_result = await self.session.execute(count_stmt)
        total = total_result.scalar_one()

        result = await self.session.execute(list_stmt)
        items = list(result.scalars().all())

        return items, total

    async def search_settings(
        self,
        search_term: str,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[Settings], int]:
        """Performs a free-text search over setting keys and descriptions.

        Args:
            search_term: Case-insensitive substring to match against the
                setting_key and description fields.
            page: 1-indexed page number.
            page_size: Number of rows per page.
            sort_by: Column name to order by.
            sort_order: ``"asc"`` or ``"desc"``.

        Returns:
            tuple[list[Settings], int]: The page of matching entries and
            the total count of matching entries.
        """
        return await self.list_settings(
            search=search_term,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )

    # ------------------------------------------------------------------
    # Scoped convenience lookups
    # ------------------------------------------------------------------

    async def get_public_settings(self) -> list[Settings]:
        """Fetches every setting entry flagged as publicly exposable.

        Returns:
            list[Settings]: All entries with ``is_public`` true, ordered by
            category then key.
        """
        result = await self.session.execute(
            select(Settings)
            .where(Settings.is_public.is_(True))
            .order_by(Settings.category.asc(), Settings.setting_key.asc())
        )
        return list(result.scalars().all())

    async def get_editable_settings(self) -> list[Settings]:
        """Fetches every setting entry flagged as editable.

        Returns:
            list[Settings]: All entries with ``is_editable`` true, ordered
            by category then key.
        """
        result = await self.session.execute(
            select(Settings)
            .where(Settings.is_editable.is_(True))
            .order_by(Settings.category.asc(), Settings.setting_key.asc())
        )
        return list(result.scalars().all())

    async def get_encrypted_settings(self) -> list[Settings]:
        """Fetches every setting entry flagged as encrypted.

        Returns:
            list[Settings]: All entries with ``is_encrypted`` true, ordered
            by category then key.
        """
        result = await self.session.execute(
            select(Settings)
            .where(Settings.is_encrypted.is_(True))
            .order_by(Settings.category.asc(), Settings.setting_key.asc())
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    async def bulk_update(
        self, updates: Sequence[tuple[uuid.UUID, dict[str, Any]]]
    ) -> list[Settings]:
        """Applies a set of per-row updates to multiple setting entries.

        Args:
            updates: Sequence of ``(setting_id, data)`` pairs, where
                ``data`` is a mapping of attribute names to new values.
                Ids that do not resolve to an existing entry are silently
                skipped.

        Returns:
            list[Settings]: The updated, refreshed entries, in the same
            relative order as the resolvable input ids.
        """
        if not updates:
            return []

        ids = [setting_id for setting_id, _ in updates]
        result = await self.session.execute(
            select(Settings).where(Settings.id.in_(ids))
        )
        entries_by_id = {entry.id: entry for entry in result.scalars().all()}

        updated: list[Settings] = []
        for setting_id, data in updates:
            entry = entries_by_id.get(setting_id)
            if entry is None:
                continue
            for field_name, value in data.items():
                if hasattr(entry, field_name):
                    setattr(entry, field_name, value)
            self.session.add(entry)
            updated.append(entry)

        await self.session.flush()
        for entry in updated:
            await self.session.refresh(entry)
        return updated

    async def bulk_delete(self, ids: Sequence[uuid.UUID]) -> int:
        """Deletes a specific set of setting entries by id.

        Args:
            ids: The primary keys of the entries to delete.

        Returns:
            int: The number of rows deleted.
        """
        if not ids:
            return 0
        stmt = delete(Settings).where(Settings.id.in_(ids))
        result = await self.session.execute(stmt)
        return result.rowcount or 0

    # ------------------------------------------------------------------
    # Dashboard / statistics aggregations
    # ------------------------------------------------------------------

    async def count_by_category(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict[str, int]:
        """Counts setting entries grouped by category.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            dict[str, int]: Mapping of category value to entry count.
        """
        stmt = self._apply_filters(
            select(Settings.category, func.count().label("count")),
            date_from=date_from,
            date_to=date_to,
        ).group_by(Settings.category)
        result = await self.session.execute(stmt)
        return {row.category.value: row.count for row in result.all()}

    async def count_by_data_type(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict[str, int]:
        """Counts setting entries grouped by data type.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            dict[str, int]: Mapping of data type value to entry count.
        """
        stmt = self._apply_filters(
            select(Settings.data_type, func.count().label("count")),
            date_from=date_from,
            date_to=date_to,
        ).group_by(Settings.data_type)
        result = await self.session.execute(stmt)
        return {row.data_type.value: row.count for row in result.all()}

    async def count_public(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> int:
        """Counts setting entries flagged as publicly exposable.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            int: The matching entry count.
        """
        stmt = self._apply_filters(
            select(func.count()).select_from(Settings),
            is_public=True,
            date_from=date_from,
            date_to=date_to,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def count_editable(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> int:
        """Counts setting entries flagged as editable.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            int: The matching entry count.
        """
        stmt = self._apply_filters(
            select(func.count()).select_from(Settings),
            is_editable=True,
            date_from=date_from,
            date_to=date_to,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def count_encrypted(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> int:
        """Counts setting entries flagged as encrypted.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            int: The matching entry count.
        """
        stmt = self._apply_filters(
            select(func.count()).select_from(Settings),
            is_encrypted=True,
            date_from=date_from,
            date_to=date_to,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def get_total_count(
        self,
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> int:
        """Counts the total number of setting entries in scope.

        Args:
            date_from: Optional inclusive lower bound on ``created_at``.
            date_to: Optional inclusive upper bound on ``created_at``.

        Returns:
            int: The total matching entry count.
        """
        stmt = self._apply_filters(
            select(func.count()).select_from(Settings),
            date_from=date_from,
            date_to=date_to,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def exists_by_category_and_key(
        self,
        category: SettingCategory,
        setting_key: str,
        *,
        exclude_id: Optional[uuid.UUID] = None,
    ) -> bool:
        """Checks whether a (category, key) pair is already in use.

        Args:
            category: The functional category to check.
            setting_key: The configuration key to check.
            exclude_id: Optional entry id to exclude from the check (used
                when validating an update against its own current row).

        Returns:
            bool: ``True`` if another entry with this (category, key)
            pair already exists, ``False`` otherwise.
        """
        conditions = [
            Settings.category == category,
            Settings.setting_key == setting_key,
        ]
        if exclude_id is not None:
            conditions.append(Settings.id != exclude_id)

        stmt = select(func.count()).select_from(Settings).where(and_(*conditions))
        result = await self.session.execute(stmt)
        return result.scalar_one() > 0