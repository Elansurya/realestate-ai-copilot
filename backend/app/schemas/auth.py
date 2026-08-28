"""
backend/app/schemas/auth.py

Pydantic v2 schemas for the Authentication & Authorization module.

Responsibilities:
    - Define request/response contracts for registration, login, and
      token refresh flows.
    - Define the internal JWT payload structure used when decoding tokens.

Design Notes:
    - These schemas are transport/validation contracts only; they contain
      no business logic, database access, or security operations (those
      live in app.core.security and the auth service layer).
    - `TokenPayload` mirrors the claims produced by
      `app.core.security.create_access_token` / `create_refresh_token`
      and is intended for use when decoding/validating JWTs, ensuring
      type-safe access to claims instead of raw dict access.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole


# --------------------------------------------------------------------------
# Register Request
# --------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    """
    Request payload for creating a new CRM user account.

    `role` defaults to the least-privileged role (`SALES_AGENT`). Per the
    security note on the `/auth/register` endpoint, exposing role
    selection here is a known trade-off for this phase and should be
    locked down (or moved to an admin-only endpoint) before production.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "full_name": "Jane Doe",
                "email": "agent@realestatecrm.com",
                "phone": "+1-555-123-4567",
                "password": "SecurePass123!",
                "role": "SALES_AGENT",
            }
        },
    )

    full_name: str = Field(
        ...,
        min_length=2,
        max_length=150,
        description="Full display name of the new user.",
        examples=["Jane Doe"],
    )
    email: EmailStr = Field(
        ...,
        description="Unique email address; used as the primary login identifier.",
        examples=["agent@realestatecrm.com"],
    )
    phone: str = Field(
        ...,
        min_length=7,
        max_length=20,
        description="Unique phone number for the new user.",
        examples=["+1-555-123-4567"],
    )
    password: str = Field(
        ...,
        min_length=8,
        description="Plaintext password (min 8 characters); hashed with bcrypt before storage.",
        examples=["SecurePass123!"],
    )
    role: UserRole = Field(
        default=UserRole.SALES_AGENT,
        description="Role assigned to the new user. Defaults to SALES_AGENT.",
        examples=["SALES_AGENT"],
    )


# --------------------------------------------------------------------------
# Login Request
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    """
    Request payload for the user login endpoint.

    Enforces a minimum password length at the schema level as a first
    line of defense; actual credential verification is performed against
    the stored bcrypt hash via `app.core.security.verify_password`.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "email": "agent@realestatecrm.com",
                "password": "SecurePass123!",
            }
        },
    )

    email: EmailStr = Field(
        ...,
        description="Registered email address of the user attempting to log in.",
        examples=["agent@realestatecrm.com"],
    )
    password: str = Field(
        ...,
        min_length=8,
        description="Plaintext password supplied by the user (min 8 characters).",
        examples=["SecurePass123!"],
    )


# --------------------------------------------------------------------------
# Token Response
# --------------------------------------------------------------------------
class TokenResponse(BaseModel):
    """
    Response payload returned upon successful authentication or token
    refresh, containing both the short-lived access token and the
    long-lived refresh token.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "token_type": "bearer",
            }
        },
    )

    access_token: str = Field(
        ...,
        description="Short-lived JWT used to authenticate subsequent API requests.",
        examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."],
    )
    refresh_token: str = Field(
        ...,
        description="Long-lived JWT used to obtain a new access token without re-login.",
        examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."],
    )
    token_type: str = Field(
        default="bearer",
        description="Type of the issued token, used in the Authorization header.",
        examples=["bearer"],
    )


# --------------------------------------------------------------------------
# Token Payload (Decoded JWT Claims)
# --------------------------------------------------------------------------
class TokenPayload(BaseModel):
    """
    Structured representation of the claims embedded within a decoded JWT.

    Used internally (e.g., in auth dependencies) to type-check and
    validate the payload returned by `app.core.security.decode_token`
    before trusting its contents.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "sub": "42",
                "type": "access",
                "exp": 1735689600,
            }
        },
    )

    sub: str = Field(
        ...,
        description="Subject claim identifying the token owner (typically the user ID).",
        examples=["42"],
    )
    type: str = Field(
        ...,
        description="Token type claim distinguishing 'access' from 'refresh' tokens.",
        examples=["access"],
    )
    exp: int = Field(
        ...,
        description="Expiration claim as a Unix timestamp (seconds since epoch).",
        examples=[1735689600],
    )


# --------------------------------------------------------------------------
# Refresh Token Request
# --------------------------------------------------------------------------
class RefreshTokenRequest(BaseModel):
    """
    Request payload for exchanging a valid refresh token for a new
    access token (and optionally a rotated refresh token).
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
            }
        },
    )

    refresh_token: str = Field(
        ...,
        description="Valid, non-expired refresh token previously issued to the user.",
        examples=["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."],
    )


__all__ = [
    "RegisterRequest",
    "LoginRequest",
    "TokenResponse",
    "TokenPayload",
    "RefreshTokenRequest",
]