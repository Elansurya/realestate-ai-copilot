"""
backend/app/services/auth_service.py

Service layer implementing authentication use cases for the Real Estate
AI Copilot CRM.

Responsibilities:
    - Orchestrate user registration, credential verification, token
      issuance, and token refresh workflows by composing
      `UserRepository` (data access) and `app.core.security`
      (cryptographic primitives).
    - Translate authentication/authorization failures into appropriate
      HTTP errors, decoupled from any specific FastAPI route definition.

Design Notes:
    - This service is framework-agnostic with respect to routing: it
      raises `HTTPException` (the standard FastAPI error vocabulary) so
      that router layers can simply propagate it, but it contains no
      route decorators, request parsing, or dependency wiring of its own.
    - No password hashing/verification or JWT encode/decode logic lives
      here directly — those operations are delegated to
      `app.core.security` to keep a single source of truth for
      cryptographic behavior.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Optional

from fastapi import HTTPException, status
from jose import JWTError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository
from app.schemas.auth import TokenResponse


class AuthService:
    """
    Service encapsulating authentication business logic: user
    registration, credential verification, login token issuance, and
    access token refresh.

    Consumed by API routers via dependency injection; contains no HTTP
    routing concerns itself.
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        Args:
            session: An active SQLAlchemy AsyncSession, typically supplied
                     via a FastAPI dependency (e.g., `get_db`).
        """
        self._session = session
        self._user_repository = UserRepository(session)

    # ----------------------------------------------------------------
    # Registration Use Case
    # ----------------------------------------------------------------
    async def register(
        self,
        *,
        full_name: str,
        email: str,
        phone: str,
        password: str,
        role: UserRole = UserRole.SALES_AGENT,
    ) -> User:
        """
        Create a new user account with a bcrypt-hashed password.

        Args:
            full_name: Full display name of the new user.
            email: Unique email address for the new user.
            phone: Unique phone number for the new user.
            password: Plaintext password to be hashed before storage.
            role: Role to assign to the new user. Defaults to
                  `UserRole.SALES_AGENT`.

        Returns:
            The newly created and persisted `User` instance.

        Raises:
            HTTPException(409): If a user with the given email or phone
                number already exists. Checked proactively for a clean
                error message, and re-checked via `IntegrityError` to
                guard against race conditions between the check and the
                insert.
        """
        existing_email = await self._user_repository.get_by_email(email)
        if existing_email is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this email already exists.",
            )

        existing_phone = await self._user_repository.get_by_phone(phone)
        if existing_phone is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this phone number already exists.",
            )

        password_hash = hash_password(password)

        try:
            user = await self._user_repository.create_user(
                uuid=str(uuid_pkg.uuid4()),
                full_name=full_name,
                email=email,
                phone=phone,
                password_hash=password_hash,
                role=role,
            )
        except IntegrityError:
            await self._session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this email or phone number already exists.",
            )

        return user

    # ----------------------------------------------------------------
    # Credential Verification
    # ----------------------------------------------------------------
    async def authenticate_user(self, email: str, password: str) -> Optional[User]:
        """
        Verify user credentials without raising exceptions on failure.

        Args:
            email: Email address supplied at login.
            password: Plaintext password supplied at login.

        Returns:
            The authenticated `User` instance if:
                - a user with the given email exists,
                - the supplied password matches the stored hash, and
                - the user account is active.
            Returns None otherwise, allowing callers to decide how to
            respond (e.g., generic 401 to avoid user enumeration).
        """
        user = await self._user_repository.get_by_email(email)
        if user is None:
            return None

        if not verify_password(password, user.password_hash):
            return None

        if not user.is_active:
            return None

        return user

    # ----------------------------------------------------------------
    # Login Use Case
    # ----------------------------------------------------------------
    async def login(self, email: str, password: str) -> TokenResponse:
        """
        Authenticate a user and issue a fresh access/refresh token pair.

        Args:
            email: Email address supplied at login.
            password: Plaintext password supplied at login.

        Returns:
            A `TokenResponse` containing the newly issued access and
            refresh tokens.

        Raises:
            HTTPException(401): If credentials are invalid, the user
                does not exist, or the account is inactive. A single
                generic message is used deliberately to avoid leaking
                which specific factor (email vs. password vs. active
                status) failed, mitigating user-enumeration attacks.
        """
        user = await self.authenticate_user(email=email, password=password)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        access_token = create_access_token(
            subject=str(user.id),
            extra_claims={"role": user.role.value},
        )
        refresh_token = create_refresh_token(subject=str(user.id))

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
        )

    # ----------------------------------------------------------------
    # Token Refresh Use Case
    # ----------------------------------------------------------------
    async def refresh_access_token(self, refresh_token: str) -> TokenResponse:
        """
        Exchange a valid, unexpired refresh token for a new access token.

        Args:
            refresh_token: The refresh token previously issued to the
                user during login.

        Returns:
            A `TokenResponse` containing a newly issued access token and
            the same refresh token passed in (refresh token rotation is
            not performed in this phase).

        Raises:
            HTTPException(401): If the refresh token is malformed,
                expired, has an invalid signature, is not of type
                "refresh", references a non-existent user, or the
                associated user account is inactive.
        """
        try:
            payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
        except (JWTError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user_id_raw = payload.get("sub")
        if user_id_raw is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            user_id = int(user_id_raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user = await self._user_repository.get_by_id(user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account is inactive.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        new_access_token = create_access_token(
            subject=str(user.id),
            extra_claims={"role": user.role.value},
        )

        return TokenResponse(
            access_token=new_access_token,
            refresh_token=refresh_token,
            token_type="bearer",
        )


__all__ = ["AuthService"]