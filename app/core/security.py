"""
backend/app/core/security.py

Enterprise-grade security utilities for the Real Estate AI Copilot CRM.

Responsibilities:
    - Password hashing & verification (bcrypt via passlib)
    - JWT access & refresh token creation
    - JWT decoding & validation

Design Notes:
    - This module is intentionally decoupled from any ORM model, schema,
      or API route. It exposes pure, reusable functions consumed by the
      authentication/authorization layer (services, dependencies, routers)
      built in later phases.
    - All configuration (secrets, algorithm, expiry durations) is sourced
      from `app.core.config.settings`, ensuring a single source of truth
      and environment-driven configuration (12-factor app compliance).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

# --------------------------------------------------------------------------
# Password Hashing Context
# --------------------------------------------------------------------------
# CryptContext manages hashing scheme(s) and deprecation policy centrally.
# bcrypt is used as the sole scheme; "auto" deprecation allows seamless
# future migration to a stronger scheme without breaking existing hashes.
# --------------------------------------------------------------------------
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# --------------------------------------------------------------------------
# Token Type Enumeration
# --------------------------------------------------------------------------
class TokenType(str, Enum):
    """
    Distinguishes access tokens from refresh tokens at the claims level.

    Embedding the token type inside the JWT payload prevents a refresh
    token from being misused as an access token (and vice versa) even
    if both are signed with the same secret/algorithm.
    """

    ACCESS = "access"
    REFRESH = "refresh"


# --------------------------------------------------------------------------
# Password Hashing Helpers
# --------------------------------------------------------------------------
def hash_password(plain_password: str) -> str:
    """
    Hash a plaintext password using bcrypt.

    Args:
        plain_password: The user-supplied plaintext password.

    Returns:
        A bcrypt hash string safe for persistent storage.

    Notes:
        - bcrypt has a 72-byte input limit; passlib handles truncation
          detection internally and raises if the password is invalid.
    """
    if not plain_password:
        raise ValueError("Password must not be empty.")
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plaintext password against a stored bcrypt hash.

    Args:
        plain_password: The plaintext password provided at login.
        hashed_password: The bcrypt hash retrieved from persistent storage.

    Returns:
        True if the password matches the hash, False otherwise.

    Notes:
        - Uses passlib's constant-time comparison internally to mitigate
          timing attacks.
        - Any malformed hash / verification error is treated as a failed
          verification rather than raising, to avoid leaking internal
          state to callers (e.g., login endpoints).
    """
    try:
        return _pwd_context.verify(plain_password, hashed_password)
    except (ValueError, TypeError):
        return False


def needs_rehash(hashed_password: str) -> bool:
    """
    Determine whether a stored hash should be regenerated (e.g., after a
    scheme/parameter upgrade). Intended to be called post-login so the
    password can be transparently re-hashed with current parameters.

    Args:
        hashed_password: The existing bcrypt hash.

    Returns:
        True if the hash should be recomputed, False otherwise.
    """
    return _pwd_context.needs_update(hashed_password)


# --------------------------------------------------------------------------
# JWT Creation Helpers
# --------------------------------------------------------------------------
def _create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta,
    extra_claims: Optional[dict[str, Any]] = None,
) -> str:
    """
    Internal helper to build and sign a JWT with standard claims.

    Standard claims included:
        sub  - subject (typically user identifier, e.g., user ID/UUID as str)
        iat  - issued-at timestamp (UTC)
        exp  - expiration timestamp (UTC)
        nbf  - not-before timestamp (UTC), equal to iat
        type - custom claim distinguishing access vs refresh tokens

    Args:
        subject: Unique identifier for the token owner (must be str).
        token_type: TokenType.ACCESS or TokenType.REFRESH.
        expires_delta: Timedelta representing token validity duration.
        extra_claims: Optional additional claims (e.g., roles, tenant_id)
                      to embed in the payload. Must not override reserved
                      claim keys (sub, iat, exp, nbf, type).

    Returns:
        Encoded JWT string.

    Raises:
        ValueError: If extra_claims attempts to override reserved claims.
    """
    reserved_keys = {"sub", "iat", "exp", "nbf", "type"}
    if extra_claims:
        conflicting = reserved_keys.intersection(extra_claims.keys())
        if conflicting:
            raise ValueError(
                f"extra_claims cannot override reserved claim(s): {conflicting}"
            )

    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": now,
        "nbf": now,
        "exp": now + expires_delta,
        "type": token_type.value,
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(
        payload,
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_access_token(
    subject: str,
    extra_claims: Optional[dict[str, Any]] = None,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a short-lived JWT access token.

    Args:
        subject: Unique identifier for the token owner (e.g., user ID).
        extra_claims: Optional additional claims (e.g., role, tenant_id).
        expires_delta: Optional override for token lifetime; defaults to
                        settings.ACCESS_TOKEN_EXPIRE_MINUTES.

    Returns:
        Encoded JWT access token string.
    """
    delta = expires_delta or timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    return _create_token(
        subject=subject,
        token_type=TokenType.ACCESS,
        expires_delta=delta,
        extra_claims=extra_claims,
    )


def create_refresh_token(
    subject: str,
    extra_claims: Optional[dict[str, Any]] = None,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a long-lived JWT refresh token.

    Args:
        subject: Unique identifier for the token owner (e.g., user ID).
        extra_claims: Optional additional claims (e.g., device_id).
        expires_delta: Optional override for token lifetime; defaults to
                        settings.REFRESH_TOKEN_EXPIRE_DAYS.

    Returns:
        Encoded JWT refresh token string.
    """
    delta = expires_delta or timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    return _create_token(
        subject=subject,
        token_type=TokenType.REFRESH,
        expires_delta=delta,
        extra_claims=extra_claims,
    )


# --------------------------------------------------------------------------
# JWT Decoding / Validation Helper
# --------------------------------------------------------------------------
def decode_token(
    token: str,
    expected_type: Optional[TokenType] = None,
) -> dict[str, Any]:
    """
    Decode and validate a JWT, returning its payload.

    Args:
        token: The encoded JWT string.
        expected_type: If provided, enforces that the token's "type" claim
                       matches (e.g., reject a refresh token used where an
                       access token is required).

    Returns:
        The decoded token payload as a dictionary.

    Raises:
        JWTError: If the token is invalid, expired, malformed, or the
                  signature verification fails. Callers (dependencies)
                  are expected to catch this and translate it into an
                  appropriate HTTP 401 response.
        ValueError: If expected_type is provided and does not match the
                    token's actual "type" claim.
    """
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )

    if expected_type is not None:
        token_type = payload.get("type")
        if token_type != expected_type.value:
            raise ValueError(
                f"Invalid token type: expected '{expected_type.value}', "
                f"got '{token_type}'."
            )

    return payload


__all__ = [
    "TokenType",
    "hash_password",
    "verify_password",
    "needs_rehash",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
]