"""
backend/app/api/dependencies/auth_dependency.py

Reusable FastAPI dependencies for authenticating incoming requests via
JWT access tokens.

Responsibilities:
    - Extract the bearer token from the `Authorization` header.
    - Decode and validate the token (signature, expiry, type) using
      `app.core.security.decode_token`.
    - Resolve the token's subject claim to a persisted `User` record.
    - Reject the request with 401 Unauthorized for any invalid, expired,
      mistyped, missing-user, or inactive-user condition.

Design Notes:
    - This module defines dependency *providers* only (`oauth2_scheme`,
      `get_current_user`). It intentionally contains no route
      definitions — routers in later phases will consume
      `get_current_user` via `Depends(...)`.
    - A single generic 401 message/exception factory is reused across
      all failure branches to avoid leaking which specific validation
      step failed (defense against information disclosure / token
      probing attacks).
    - `get_current_active_user` is provided as a thin, explicit
      convenience layer for routes that want to be unambiguous about
      requiring an active account, even though `get_current_user`
      already enforces this invariant.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenType, decode_token
from app.db.session import get_db
from app.models.user import User
from app.repositories.user_repository import UserRepository

# --------------------------------------------------------------------------
# OAuth2 Scheme
# --------------------------------------------------------------------------
# `tokenUrl` points to the (future) login endpoint. It is used solely to
# populate OpenAPI/Swagger UI's "Authorize" flow metadata — FastAPI does
# not call this URL itself. Actual authentication happens in
# `get_current_user` below via `decode_token`.
# --------------------------------------------------------------------------
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/v1/auth/login",
    description="JWT access token obtained from the login endpoint.",
)


def _unauthorized(detail: str = "Could not validate credentials.") -> HTTPException:
    """
    Build a standardized 401 Unauthorized exception.

    Centralizing this ensures every authentication failure branch below
    returns an identical status code, header, and (by default) message,
    preventing subtle inconsistencies that could aid an attacker in
    distinguishing failure reasons.

    Args:
        detail: Optional override for the error detail message.

    Returns:
        A configured `HTTPException` ready to be raised.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Resolve and validate the currently authenticated user from the
    request's bearer access token.

    Validation pipeline:
        1. Decode the JWT (signature + expiry verified by `decode_token`).
        2. Enforce that the token's `type` claim is `TokenType.ACCESS`
           (rejects refresh tokens presented as access tokens).
        3. Parse the `sub` claim as the internal integer user ID.
        4. Fetch the corresponding `User` record from the database.
        5. Enforce that the user account is active.

    Args:
        token: The raw bearer token string, extracted automatically from
               the `Authorization: Bearer <token>` header by
               `oauth2_scheme`.
        db: An active `AsyncSession`, injected via `get_db`.

    Returns:
        The authenticated and active `User` instance.

    Raises:
        HTTPException(401): If the token is malformed, has an invalid
            signature, is expired, is not an access token, does not
            resolve to an existing user, or the user account is
            inactive.
    """
    try:
        payload = decode_token(token, expected_type=TokenType.ACCESS)
    except (JWTError, ValueError):
        raise _unauthorized()

    subject = payload.get("sub")
    if subject is None:
        raise _unauthorized()

    try:
        user_id = int(subject)
    except (TypeError, ValueError):
        raise _unauthorized()

    user_repository = UserRepository(db)
    user = await user_repository.get_by_id(user_id)

    if user is None:
        raise _unauthorized()

    if not user.is_active:
        raise _unauthorized(detail="User account is inactive.")

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Convenience dependency for routes that want an explicit, self-
    documenting requirement of an active authenticated user.

    Since `get_current_user` already rejects inactive accounts, this
    dependency is a semantic pass-through — it exists so route
    signatures can read `Depends(get_current_active_user)` for clarity
    in endpoints where "active" status is a critical, load-bearing
    precondition (e.g., financial transactions, deal approvals).

    Args:
        current_user: The authenticated `User`, resolved and validated
                       by `get_current_user`.

    Returns:
        The authenticated, active `User` instance.
    """
    return current_user


__all__ = [
    "oauth2_scheme",
    "get_current_user",
    "get_current_active_user",
]