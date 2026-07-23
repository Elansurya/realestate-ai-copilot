"""
backend/app/schemas/user.py

Pydantic v2 schemas for representing User data across API boundaries.

Responsibilities:
    - Define the safe, outward-facing representation of a `User` for API
      responses (e.g., `/auth/me`, future user-management endpoints).

Design Notes:
    - `UserResponse` deliberately excludes sensitive fields such as
      `password_hash`. Only fields safe for client consumption are
      exposed, preventing accidental leakage of credential material.
    - `ConfigDict(from_attributes=True)` allows this schema to be
      constructed directly from a SQLAlchemy `User` ORM instance
      (`UserResponse.model_validate(user)` or, with FastAPI's
      `response_model`, automatic serialization of the returned ORM
      object) without manual dict conversion.
"""

from __future__ import annotations

from datetime import datetime

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


__all__ = ["UserResponse"]