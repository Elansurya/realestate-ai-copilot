"""
Property Repository
=====================

Data-access layer for the Property Management module.

Follows the same conventions established in `app/repositories/user_repository.py`:
- Async SQLAlchemy 2.x (`AsyncSession`, `select`, `func`)
- Repository Pattern — no business logic here, only persistence/query concerns
- Methods return ORM model instances (or primitives for counts); the Service
  layer is responsible for mapping to Pydantic schemas
- Soft delete via `is_active` flag rather than physical row deletion
"""

import uuid
from typing import Optional, Sequence

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.property import (
    ListingType,
    Property,
    PropertyStatus,
    PropertyType,
)

# --------------------------------------------------------------------------
# Allow-listed sort columns
# --------------------------------------------------------------------------

SORTABLE_FIELDS: dict[str, str] = {
    "created_at": "created_at",
    "updated_at": "updated_at",
    "price": "price",
    "area_sqft": "area_sqft",
    "title": "title",
    "city": "city",
}
DEFAULT_SORT_FIELD = "created_at"


class PropertyRepository:
    """Encapsulates all database access for the `Property` model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ----------------------------------------------------------------
    # Create
    # ----------------------------------------------------------------

    async def create(self, property_obj: Property) -> Property:
        """Persist a new `Property` instance."""
        self.session.add(property_obj)
        await self.session.flush()
        await self.session.refresh(property_obj)
        return property_obj

    # ----------------------------------------------------------------
    # Read — single record lookups
    # ----------------------------------------------------------------

    async def get_by_id(self, property_id: int) -> Optional[Property]:
        """Fetch a property by internal integer id."""
        stmt = select(Property).where(Property.id == property_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_uuid(self, property_uuid: uuid.UUID) -> Optional[Property]:
        """Fetch a property by its public UUID."""
        stmt = select(Property).where(Property.uuid == property_uuid)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_property_code(self, property_code: str) -> Optional[Property]:
        """Fetch a property by its unique human-readable code."""
        stmt = select(Property).where(Property.property_code == property_code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def exists_by_property_code(self, property_code: str) -> bool:
        """Check whether a property_code is already in use."""
        stmt = select(func.count()).select_from(Property).where(
            Property.property_code == property_code
        )
        result = await self.session.execute(stmt)
        return (result.scalar_one() or 0) > 0

    # ----------------------------------------------------------------
    # Update
    # ----------------------------------------------------------------

    async def update(self, property_obj: Property, data: dict) -> Property:
        """Apply a dict of column updates to an existing property instance."""
        for field, value in data.items():
            setattr(property_obj, field, value)
        await self.session.flush()
        await self.session.refresh(property_obj)
        return property_obj

    async def update_status(
        self, property_obj: Property, property_status: PropertyStatus
    ) -> Property:
        """Update only the property_status field."""
        property_obj.property_status = property_status
        await self.session.flush()
        await self.session.refresh(property_obj)
        return property_obj

    async def assign_agent(
        self, property_obj: Property, assigned_agent_id: int
    ) -> Property:
        """Assign (or reassign) the property to an agent."""
        property_obj.assigned_agent_id = assigned_agent_id
        await self.session.flush()
        await self.session.refresh(property_obj)
        return property_obj

    # ----------------------------------------------------------------
    # Delete (soft)
    # ----------------------------------------------------------------

    async def soft_delete(self, property_obj: Property) -> Property:
        """Deactivate a property record without physically deleting it."""
        property_obj.is_active = False
        await self.session.flush()
        await self.session.refresh(property_obj)
        return property_obj

    async def reactivate(self, property_obj: Property) -> Property:
        """Reactivate a previously soft-deleted property record."""
        property_obj.is_active = True
        await self.session.flush()
        await self.session.refresh(property_obj)
        return property_obj

    # ----------------------------------------------------------------
    # Query building — search, filters, sorting, pagination
    # ----------------------------------------------------------------

    def _base_query(self) -> Select:
        """Base SELECT with eager-loaded relationships."""
        return select(Property).options(selectinload(Property.assigned_agent))

    def _apply_search(self, stmt: Select, search: Optional[str]) -> Select:
        """
        Apply a case-insensitive partial-match search across:
        property_code, title, city, state, owner_name.
        """
        if not search:
            return stmt
        pattern = f"%{search.strip()}%"
        return stmt.where(
            or_(
                Property.property_code.ilike(pattern),
                Property.title.ilike(pattern),
                Property.city.ilike(pattern),
                Property.state.ilike(pattern),
                Property.owner_name.ilike(pattern),
            )
        )

    def _apply_filters(
        self,
        stmt: Select,
        *,
        property_type: Optional[PropertyType] = None,
        property_status: Optional[PropertyStatus] = None,
        listing_type: Optional[ListingType] = None,
        assigned_agent_id: Optional[int] = None,
        is_featured: Optional[bool] = None,
        is_active: Optional[bool] = None,
    ) -> Select:
        """Apply optional equality filters. `None` means 'do not filter'."""
        if property_type is not None:
            stmt = stmt.where(Property.property_type == property_type)
        if property_status is not None:
            stmt = stmt.where(Property.property_status == property_status)
        if listing_type is not None:
            stmt = stmt.where(Property.listing_type == listing_type)
        if assigned_agent_id is not None:
            stmt = stmt.where(Property.assigned_agent_id == assigned_agent_id)
        if is_featured is not None:
            stmt = stmt.where(Property.is_featured == is_featured)
        if is_active is not None:
            stmt = stmt.where(Property.is_active == is_active)
        return stmt

    def _apply_sorting(
        self, stmt: Select, sort_by: str, sort_order: str
    ) -> Select:
        """
        Apply sorting on an allow-listed column, defaulting to `created_at`
        if an unrecognized field is supplied (defensive against injection
        via arbitrary attribute names).
        """
        column_name = SORTABLE_FIELDS.get(sort_by, SORTABLE_FIELDS[DEFAULT_SORT_FIELD])
        column = getattr(Property, column_name)
        if sort_order.lower() == "asc":
            return stmt.order_by(column.asc())
        return stmt.order_by(column.desc())

    # ----------------------------------------------------------------
    # List / paginate / count
    # ----------------------------------------------------------------

    async def list_properties(
        self,
        *,
        search: Optional[str] = None,
        property_type: Optional[PropertyType] = None,
        property_status: Optional[PropertyStatus] = None,
        listing_type: Optional[ListingType] = None,
        assigned_agent_id: Optional[int] = None,
        is_featured: Optional[bool] = None,
        is_active: Optional[bool] = True,
        sort_by: str = DEFAULT_SORT_FIELD,
        sort_order: str = "desc",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[Sequence[Property], int]:
        """
        Return a page of properties matching the given search/filter criteria,
        along with the total matching count (pre-pagination).

        Defaults to `is_active=True` so soft-deleted properties are excluded
        unless explicitly requested via `is_active=None` or `is_active=False`.
        """
        stmt = self._base_query()
        stmt = self._apply_search(stmt, search)
        stmt = self._apply_filters(
            stmt,
            property_type=property_type,
            property_status=property_status,
            listing_type=listing_type,
            assigned_agent_id=assigned_agent_id,
            is_featured=is_featured,
            is_active=is_active,
        )

        total = await self._count(stmt)

        stmt = self._apply_sorting(stmt, sort_by, sort_order)
        offset = (max(page, 1) - 1) * page_size
        stmt = stmt.offset(offset).limit(page_size)

        result = await self.session.execute(stmt)
        items = result.scalars().all()
        return items, total

    async def _count(self, stmt: Select) -> int:
        """Return the total row count for a given (unpaginated) SELECT statement."""
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self.session.execute(count_stmt)
        return result.scalar_one() or 0

    async def count_by_status(self, property_status: PropertyStatus) -> int:
        """Count active properties in a given status (e.g. for dashboard widgets)."""
        stmt = (
            select(func.count())
            .select_from(Property)
            .where(
                Property.property_status == property_status,
                Property.is_active.is_(True),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one() or 0

    async def count_by_agent(self, assigned_agent_id: int) -> int:
        """Count active properties currently assigned to a given agent."""
        stmt = (
            select(func.count())
            .select_from(Property)
            .where(
                Property.assigned_agent_id == assigned_agent_id,
                Property.is_active.is_(True),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one() or 0