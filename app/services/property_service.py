"""
Property Service
==================

Business/service layer for the Property Management module.

Follows the same conventions established in `app/services/user_service.py`:
- Orchestrates repository calls; contains all business rules and validation
- Raises `fastapi.HTTPException` for client-facing error conditions
- Owns transaction boundaries (`commit` on success, `rollback` on failure) —
  the repository layer only `flush`es, it never commits
- Converts ORM instances to Pydantic response schemas at the boundary
"""

import logging
import random
import string
import uuid
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.property import (
    ListingType,
    Property,
    PropertyStatus,
    PropertyType,
)
from app.repositories.property_repository import PropertyRepository
from app.repositories.user_repository import UserRepository
from app.schemas.property import (
    PaginatedPropertyResponse,
    PropertyAssignment,
    PropertyCreate,
    PropertyResponse,
    PropertyStatusUpdate,
    PropertyUpdate,
)

# --------------------------------------------------------------------------
# Business constants
# --------------------------------------------------------------------------

PROPERTY_CODE_PREFIX = "PROP"
PROPERTY_CODE_RANDOM_LENGTH = 6
PROPERTY_CODE_MAX_GENERATION_ATTEMPTS = 5

# Statuses considered "terminal" — cannot be transitioned out of once reached
TERMINAL_STATUSES = {PropertyStatus.SOLD, PropertyStatus.RENTED}

# Listing types paired with their disallowed terminal status
INVALID_STATUS_FOR_LISTING = {
    ListingType.SALE: PropertyStatus.RENTED,
    ListingType.RENT: PropertyStatus.SOLD,
}


class PropertyService:
    """Encapsulates business logic for property management."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = PropertyRepository(session)
        self.user_repository = UserRepository(session)

    # ----------------------------------------------------------------
    # Create
    # ----------------------------------------------------------------

    async def create_property(self, payload: PropertyCreate) -> PropertyResponse:
        """
        Create a new property listing.

        Business rules:
        - `property_code` is auto-generated when not supplied.
        - If supplied, `property_code` must be unique.
        - `assigned_agent_id`, if supplied, must reference an existing user.
        - `property_status` must be compatible with `listing_type`
          (e.g. a listing_type=SALE property cannot start as RENTED).
        """
        try:
            if payload.assigned_agent_id is not None:
                await self._ensure_agent_exists(payload.assigned_agent_id)

            self._validate_status_listing_compatibility(
                payload.listing_type, payload.property_status
            )

            property_code = await self._resolve_property_code(payload.property_code)

            property_obj = Property(
                property_code=property_code,
                title=payload.title,
                description=payload.description,
                property_type=payload.property_type,
                property_status=payload.property_status,
                listing_type=payload.listing_type,
                furnishing=payload.furnishing,
                price=payload.price,
                area_sqft=payload.area_sqft,
                bedrooms=payload.bedrooms,
                bathrooms=payload.bathrooms,
                parking=payload.parking,
                address=payload.address,
                city=payload.city,
                state=payload.state,
                pincode=payload.pincode,
                latitude=payload.latitude,
                longitude=payload.longitude,
                assigned_agent_id=payload.assigned_agent_id,
                owner_name=payload.owner_name,
                owner_phone=payload.owner_phone,
                owner_email=payload.owner_email,
                is_featured=payload.is_featured,
            )

            property_obj = await self.repository.create(property_obj)
            await self.session.commit()
            await self.session.refresh(property_obj)
            return PropertyResponse.model_validate(property_obj)

        except HTTPException:
            await self.session.rollback()
            raise
        except Exception as exc:
            logger.exception("Failed to create property")
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create property.",
            ) from exc

    # ----------------------------------------------------------------
    # Read
    # ----------------------------------------------------------------

    async def get_property(self, property_uuid: uuid.UUID) -> PropertyResponse:
        """Fetch a single property by UUID, or raise 404 if not found."""
        property_obj = await self._get_property_or_404(property_uuid)
        return PropertyResponse.model_validate(property_obj)

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
        sort_by: str = "created_at",
        sort_order: str = "desc",
        page: int = 1,
        page_size: int = 20,
    ) -> PaginatedPropertyResponse:
        """
        Return a paginated, filtered, sorted list of properties.

        `page` and `page_size` are clamped to sane bounds to protect against
        abusive or accidental oversized queries.
        """
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)

        items, total = await self.repository.list_properties(
            search=search,
            property_type=property_type,
            property_status=property_status,
            listing_type=listing_type,
            assigned_agent_id=assigned_agent_id,
            is_featured=is_featured,
            is_active=is_active,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )

        total_pages = (total + page_size - 1) // page_size if total else 0

        return PaginatedPropertyResponse(
            items=[PropertyResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    # ----------------------------------------------------------------
    # Update
    # ----------------------------------------------------------------

    async def update_property(
        self, property_uuid: uuid.UUID, payload: PropertyUpdate
    ) -> PropertyResponse:
        """
        Partially update a property. Only fields explicitly set on the
        payload are applied.
        """
        try:
            property_obj = await self._get_property_or_404(property_uuid)

            update_data = payload.model_dump(exclude_unset=True)

            if "property_code" in update_data:
                # property_code is immutable post-creation; ignore silently
                # rather than error, to keep PATCH semantics forgiving.
                update_data.pop("property_code")

            resulting_listing_type = update_data.get(
                "listing_type", property_obj.listing_type
            )
            resulting_status = update_data.get(
                "property_status", property_obj.property_status
            )
            self._validate_status_listing_compatibility(
                resulting_listing_type, resulting_status
            )

            if not update_data:
                return PropertyResponse.model_validate(property_obj)

            property_obj = await self.repository.update(property_obj, update_data)
            await self.session.commit()
            await self.session.refresh(property_obj)
            return PropertyResponse.model_validate(property_obj)

        except HTTPException:
            await self.session.rollback()
            raise
        except Exception as exc:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update property.",
            ) from exc

    async def update_status(
        self, property_uuid: uuid.UUID, payload: PropertyStatusUpdate
    ) -> PropertyResponse:
        """
        Update a property's status, enforcing valid state transitions.

        Business rules:
        - Terminal statuses (SOLD, RENTED) cannot transition to another
          status once set — the listing must be reactivated by an explicit
          admin action outside this endpoint (e.g. re-listing as a new
          property), not by a plain status update.
        - A status must remain compatible with the property's listing_type.
        """
        try:
            property_obj = await self._get_property_or_404(property_uuid)

            if property_obj.property_status in TERMINAL_STATUSES:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Property is already '{property_obj.property_status.value}' "
                        "and cannot transition to another status."
                    ),
                )

            self._validate_status_listing_compatibility(
                property_obj.listing_type, payload.property_status
            )

            property_obj = await self.repository.update_status(
                property_obj, payload.property_status
            )
            await self.session.commit()
            await self.session.refresh(property_obj)
            return PropertyResponse.model_validate(property_obj)

        except HTTPException:
            await self.session.rollback()
            raise
        except Exception as exc:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update property status.",
            ) from exc

    async def assign_agent(
        self, property_uuid: uuid.UUID, payload: PropertyAssignment
    ) -> PropertyResponse:
        """Assign or reassign a property to an agent."""
        try:
            property_obj = await self._get_property_or_404(property_uuid)
            await self._ensure_agent_exists(payload.assigned_agent_id)

            property_obj = await self.repository.assign_agent(
                property_obj, payload.assigned_agent_id
            )
            await self.session.commit()
            await self.session.refresh(property_obj)
            return PropertyResponse.model_validate(property_obj)

        except HTTPException:
            await self.session.rollback()
            raise
        except Exception as exc:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to assign agent to property.",
            ) from exc

    # ----------------------------------------------------------------
    # Delete (soft) / Reactivate
    # ----------------------------------------------------------------

    async def soft_delete_property(self, property_uuid: uuid.UUID) -> PropertyResponse:
        """Deactivate a property (soft delete). Already-inactive is a no-op success."""
        try:
            property_obj = await self._get_property_or_404(property_uuid)

            if not property_obj.is_active:
                return PropertyResponse.model_validate(property_obj)

            property_obj = await self.repository.soft_delete(property_obj)
            await self.session.commit()
            await self.session.refresh(property_obj)
            return PropertyResponse.model_validate(property_obj)

        except HTTPException:
            await self.session.rollback()
            raise
        except Exception as exc:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to deactivate property.",
            ) from exc

    async def reactivate_property(self, property_uuid: uuid.UUID) -> PropertyResponse:
        """Reactivate a previously soft-deleted property. Already-active is a no-op success."""
        try:
            property_obj = await self._get_property_or_404(property_uuid)

            if property_obj.is_active:
                return PropertyResponse.model_validate(property_obj)

            property_obj = await self.repository.reactivate(property_obj)
            await self.session.commit()
            await self.session.refresh(property_obj)
            return PropertyResponse.model_validate(property_obj)

        except HTTPException:
            await self.session.rollback()
            raise
        except Exception as exc:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to reactivate property.",
            ) from exc

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------

    async def _get_property_or_404(self, property_uuid: uuid.UUID) -> Property:
        """Fetch a property by UUID or raise 404."""
        property_obj = await self.repository.get_by_uuid(property_uuid)
        if property_obj is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Property not found.",
            )
        return property_obj

    async def _ensure_agent_exists(self, agent_id: int) -> None:
        """Validate that the given agent id references an existing, active user."""
        agent = await self.user_repository.get_by_id(agent_id)
        if agent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agent with id {agent_id} not found.",
            )
        if hasattr(agent, "is_active") and not agent.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot assign property to an inactive agent.",
            )

    def _validate_status_listing_compatibility(
        self, listing_type: ListingType, property_status: PropertyStatus
    ) -> None:
        """Ensure the property_status is valid for the given listing_type."""
        disallowed_status = INVALID_STATUS_FOR_LISTING.get(listing_type)
        if disallowed_status is not None and property_status == disallowed_status:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"property_status '{property_status.value}' is not valid "
                    f"for listing_type '{listing_type.value}'."
                ),
            )

    async def _resolve_property_code(self, supplied_code: Optional[str]) -> str:
        """
        Return a valid, unique property_code.

        If the caller supplied one, verify it is unique. Otherwise, generate
        a new code in the form `PROP-<YEAR>-<RANDOM>` and retry on collision
        up to `PROPERTY_CODE_MAX_GENERATION_ATTEMPTS` times.
        """
        if supplied_code:
            if await self.repository.exists_by_property_code(supplied_code):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"property_code '{supplied_code}' is already in use.",
                )
            return supplied_code

        for _ in range(PROPERTY_CODE_MAX_GENERATION_ATTEMPTS):
            candidate = self._generate_property_code()
            if not await self.repository.exists_by_property_code(candidate):
                return candidate

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate a unique property_code. Please retry.",
        )

    @staticmethod
    def _generate_property_code() -> str:
        """Generate a property code in the form PROP-YYYY-XXXXXX."""
        year = datetime.utcnow().year
        random_suffix = "".join(
            random.choices(string.ascii_uppercase + string.digits, k=PROPERTY_CODE_RANDOM_LENGTH)
        )
        return f"{PROPERTY_CODE_PREFIX}-{year}-{random_suffix}"