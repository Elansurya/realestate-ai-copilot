"""
backend/app/schemas/user.py

Pydantic v2 schemas for representing User data across API boundaries.

Responsibilities:
    - Define the safe, outward-facing representation of a `User` for API
      responses (e.g., `/auth/me`, future user-management endpoints).
    - Define request contracts for self-service profile updates,
      admin-driven user management updates, password changes, and
      paginated user listings (Milestone 4: User Management Module).

Design Notes:
    - `UserResponse` deliberately excludes sensitive fields such as
      `password_hash`. Only fields safe for client consumption are
      exposed, preventing accidental leakage of credential material.
    - `ConfigDict(from_attributes=True)` allows this schema to be
      constructed directly from a SQLAlchemy `User` ORM instance
      (`UserResponse.model_validate(user)` or, with FastAPI's
      `response_model`, automatic serialization of the returned ORM
      object) without manual dict conversion.
    - `UserUpdate` (self-service) and `UserAdminUpdate` (admin-only) are
      intentionally SEPARATE schemas rather than one shared schema with
      optional privileged fields. This is a deliberate security
      boundary enforced at the schema layer: even if a route handler
      were to forget an explicit permission check, `UserUpdate` has no
      `role` or `email` field to bind to in the first place, so
      privilege escalation via mass-assignment is structurally
      impossible through that path.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole


class UserResponse(BaseModel):
    """
    Outward-facing representation of a CRM user, returned by
    authentication and user-management endpoints.

    Excludes all security-sensitive fields (e.g., `password_hash`),
    ensuring credential material never leaves the server boundary.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "uuid": "a3f1c9e2-4b5d-4e6f-8a1b-2c3d4e5f6a7b",
                "full_name": "Jane Doe",
                "email": "agent@realestatecrm.com",
                "phone": "+1-555-123-4567",
                "role": "SALES_AGENT",
                "is_active": True,
                "is_verified": True,
                "created_at": "2025-01-15T10:30:00Z",
                "updated_at": "2025-01-15T10:30:00Z",
            }
        },
    )

    uuid: str = Field(
        ...,
        description="Public, non-sequential unique identifier for the user.",
        examples=["a3f1c9e2-4b5d-4e6f-8a1b-2c3d4e5f6a7b"],
    )
    full_name: str = Field(
        ...,
        description="User's full display name.",
        examples=["Jane Doe"],
    )
    email: EmailStr = Field(
        ...,
        description="User's registered email address.",
        examples=["agent@realestatecrm.com"],
    )
    phone: str = Field(
        ...,
        description="User's registered phone number.",
        examples=["+1-555-123-4567"],
    )
    role: UserRole = Field(
        ...,
        description="Role governing the user's permissions within the CRM.",
        examples=["SALES_AGENT"],
    )
    is_active: bool = Field(
        ...,
        description="Whether the user's account is currently active.",
        examples=[True],
    )
    is_verified: bool = Field(
        ...,
        description="Whether the user has verified their email/phone.",
        examples=[True],
    )
    created_at: datetime = Field(
        ...,
        description="UTC timestamp when the user record was created.",
        examples=["2025-01-15T10:30:00Z"],
    )
    updated_at: datetime = Field(
        ...,
        description="UTC timestamp when the user record was last updated.",
        examples=["2025-01-15T10:30:00Z"],
    )


# --------------------------------------------------------------------------
# Self-Service Profile Update Request
# --------------------------------------------------------------------------
class UserUpdate(BaseModel):
    """
    Request payload for a user updating their OWN profile.

    Security boundary:
        This schema intentionally has NO `role` and NO `email` field.
        Self-service callers can never change their own role
        (privilege escalation) or email (primary login identifier /
        identity-verification concern) through this endpoint, even by
        accident — the fields simply do not exist to bind to.

        Email changes, if required in the future, should be handled by
        a dedicated, separately-verified flow (e.g., confirmation link
        to the new address) rather than a plain profile PATCH.

    All fields are optional to support partial updates (`PATCH`
    semantics) — a caller may update only `full_name`, only `phone`,
    or both in a single request.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "full_name": "Jane A. Doe",
                "phone": "+1-555-987-6543",
            }
        },
    )

    full_name: Optional[str] = Field(
        default=None,
        min_length=2,
        max_length=150,
        description="Updated full display name. Omit to leave unchanged.",
        examples=["Jane A. Doe"],
    )
    phone: Optional[str] = Field(
        default=None,
        min_length=7,
        max_length=20,
        description="Updated phone number. Omit to leave unchanged.",
        examples=["+1-555-987-6543"],
    )


# --------------------------------------------------------------------------
# Admin-Driven User Update Request
# --------------------------------------------------------------------------
class UserAdminUpdate(BaseModel):
    """
    Request payload for an ADMIN updating another user's role and/or
    account status.

    Security boundary:
        This schema is intentionally separate from `UserUpdate`. It is
        the only schema that permits `role`, `is_active`, and
        `is_verified` changes, and it must only ever be bound on routes
        protected by an admin-only authorization dependency (see
        `app.dependencies.rbac.require_roles`). It still excludes
        `email`, `phone`, and `password_hash` — administrative role/
        status management is kept separate from profile-data edits and
        credential management, each of which has its own narrower
        endpoint and audit surface.

    All fields are optional to support partial updates — an admin may,
    for example, change only `is_active` (deactivate) without touching
    `role`.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "role": "SALES_MANAGER",
                "is_active": True,
                "is_verified": True,
            }
        },
    )

    role: Optional[UserRole] = Field(
        default=None,
        description="Updated role to assign to the user. Omit to leave unchanged.",
        examples=["SALES_MANAGER"],
    )
    is_active: Optional[bool] = Field(
        default=None,
        description=(
            "Updated active/inactive status. Setting to False disables "
            "authentication for the user. Omit to leave unchanged."
        ),
        examples=[True],
    )
    is_verified: Optional[bool] = Field(
        default=None,
        description="Updated email/phone verification status. Omit to leave unchanged.",
        examples=[True],
    )


# --------------------------------------------------------------------------
# Change Password Request
# --------------------------------------------------------------------------
class ChangePasswordRequest(BaseModel):
    """
    Request payload for a user changing their OWN password.

    Requires the current plaintext password (`old_password`) for
    re-authentication before the change is accepted — the service layer
    is responsible for verifying it against the stored bcrypt hash via
    `app.core.security` before persisting `new_password`. This schema
    performs no verification itself; it only validates shape and the
    minimum length policy.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "old_password": "CurrentSecurePass123!",
                "new_password": "NewSecurePass456!",
            }
        },
    )

    old_password: str = Field(
        ...,
        min_length=8,
        description="The user's current plaintext password, for re-authentication.",
        examples=["CurrentSecurePass123!"],
    )
    new_password: str = Field(
        ...,
        min_length=8,
        description="The new plaintext password (min 8 characters); hashed before storage.",
        examples=["NewSecurePass456!"],
    )


# --------------------------------------------------------------------------
# Paginated User List Response
# --------------------------------------------------------------------------
class PaginatedUserResponse(BaseModel):
    """
    Paginated envelope wrapping a page of `UserResponse` items, returned
    by admin user-listing endpoints.

    Reuses `UserResponse` for each item rather than duplicating user
    fields, keeping a single source of truth for the outward-facing
    user representation.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [
                    {
                        "uuid": "a3f1c9e2-4b5d-4e6f-8a1b-2c3d4e5f6a7b",
                        "full_name": "Jane Doe",
                        "email": "agent@realestatecrm.com",
                        "phone": "+1-555-123-4567",
                        "role": "SALES_AGENT",
                        "is_active": True,
                        "is_verified": True,
                        "created_at": "2025-01-15T10:30:00Z",
                        "updated_at": "2025-01-15T10:30:00Z",
                    }
                ],
                "total": 42,
                "page": 1,
                "page_size": 20,
            }
        },
    )

    items: List[UserResponse] = Field(
        ...,
        description="The page of user records matching the current filters.",
    )
    total: int = Field(
        ...,
        ge=0,
        description="Total number of user records matching the current filters, across all pages.",
        examples=[42],
    )
    page: int = Field(
        ...,
        ge=1,
        description="The current 1-indexed page number.",
        examples=[1],
    )
    page_size: int = Field(
        ...,
        ge=1,
        description="The maximum number of items requested per page.",
        examples=[20],
    )


__all__ = [
    "UserResponse",
    "UserUpdate",
    "UserAdminUpdate",
    "ChangePasswordRequest",
    "PaginatedUserResponse",
]