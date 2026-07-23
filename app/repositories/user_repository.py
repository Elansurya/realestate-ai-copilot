"""
backend/app/repositories/user_repository.py

Data-access layer for the User entity.

Responsibilities:
    - Encapsulate all direct database interactions (SELECT/INSERT/UPDATE/
      DELETE) for the `User` model behind a clean, testable interface.
    - Provide a single point of change if the persistence mechanism or
      query strategy evolves (e.g., adding caching, read replicas).

Design Notes:
    - This repository is strictly a data-access abstraction. It contains
      NO business rules (e.g., password hashing, uniqueness policy
      decisions, authentication logic) — those belong in the service
      layer, which composes repository calls to implement use cases.
    - All methods are async and expect an `AsyncSession` injected via the
      constructor (typically provided by a FastAPI dependency in later
      phases).
    - Methods that mutate state (`create`, `create_user`, `update`,
      `delete`) commit and refresh the session so callers immediately
      receive a fully up-to-date ORM instance (with server-generated
      defaults such as `id`, `created_at`, etc. populated).

Temporary Debugging Note:
    - `create()` currently wraps its database operations in a
      try/except block that prints the exception type, message, and
      full traceback to the terminal before re-raising. This is a
      TEMPORARY diagnostic measure to surface the root cause of a 500
      error on user registration and should be removed once the
      underlying issue is identified and fixed.
"""

from __future__ import annotations

import traceback
from typing import Optional, Sequence

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole


class UserRepository:
    """
    Repository encapsulating CRUD operations for the `User` model.

    Consumed by higher-level services (e.g., AuthService, UserService)
    which orchestrate business logic on top of these primitive
    persistence operations.
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        Args:
            session: An active SQLAlchemy AsyncSession, typically supplied
                     via a FastAPI dependency (e.g., `get_db`).
        """
        self._session = session

    # ----------------------------------------------------------------
    # Read Operations
    # ----------------------------------------------------------------
    async def get_by_email(self, email: str) -> Optional[User]:
        """
        Retrieve a user by their unique email address.

        Args:
            email: The email address to search for.

        Returns:
            The matching `User` instance, or None if no match is found.
        """
        stmt = select(User).where(User.email == email)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> Optional[User]:
        """
        Retrieve a user by their unique phone number.

        Args:
            phone: The phone number to search for.

        Returns:
            The matching `User` instance, or None if no match is found.
        """
        stmt = select(User).where(User.phone == phone)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: int) -> Optional[User]:
        """
        Retrieve a user by their internal surrogate primary key.

        Args:
            user_id: The internal integer ID of the user.

        Returns:
            The matching `User` instance, or None if no match is found.
        """
        stmt = select(User).where(User.id == user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_uuid(self, uuid: str) -> Optional[User]:
        """
        Retrieve a user by their public-facing UUID identifier.

        Args:
            uuid: The public UUID string of the user.

        Returns:
            The matching `User` instance, or None if no match is found.
        """
        stmt = select(User).where(User.uuid == uuid)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_users(self, skip: int = 0, limit: int = 100) -> Sequence[User]:
        """
        Retrieve a paginated list of users, ordered by primary key.

        Args:
            skip: Number of records to skip (offset), for pagination.
            limit: Maximum number of records to return.

        Returns:
            A sequence of `User` instances (may be empty).
        """
        stmt = select(User).order_by(User.id).offset(skip).limit(limit)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    # ----------------------------------------------------------------
    # Write Operations
    # ----------------------------------------------------------------
    async def create(self, user: User) -> User:
        """
        Persist a new user record.

        Args:
            user: A transient `User` instance (not yet added to the
                  session) with all required fields populated by the
                  caller (e.g., service layer has already hashed the
                  password and generated a UUID).

        Returns:
            The persisted `User` instance, refreshed with any
            server-generated values (id, created_at, updated_at, etc.).

        Notes:
            - TEMPORARY DEBUGGING: database operations are wrapped in a
              try/except block that prints the exception type, message,
              and full traceback to the terminal, rolls back the
              session, and re-raises the original exception unchanged.
              This block should be removed once the root cause of the
              current registration 500 error is identified and fixed.
        """
        try:
            self._session.add(user)
            await self._session.commit()
            await self._session.refresh(user)
        except Exception as exc:
            await self._session.rollback()
            print("CREATE USER ERROR")
            print(f"Exception type: {type(exc)}")
            print(f"Exception message: {exc}")
            traceback.print_exc()
            raise
        return user

    async def create_user(
        self,
        *,
        uuid: str,
        full_name: str,
        email: str,
        phone: str,
        password_hash: str,
        role: UserRole,
    ) -> User:
        """
        Construct and persist a new `User` record.

        Thin convenience wrapper around `create()` that assembles the
        `User` entity from primitive fields, keeping entity construction
        out of the service layer's business logic.

        Args:
            uuid: Pre-generated public UUID for the new user.
            full_name: User's full display name.
            email: Unique email address (uniqueness assumed already
                   checked by the caller).
            phone: Unique phone number (uniqueness assumed already
                   checked by the caller).
            password_hash: Bcrypt hash of the user's password (never the
                   plaintext).
            role: The `UserRole` to assign to the new user.

        Returns:
            The persisted `User` instance, refreshed with
            server-generated values (id, created_at, updated_at).
        """
        user = User(
            uuid=uuid,
            full_name=full_name,
            email=email,
            phone=phone,
            password_hash=password_hash,
            role=role,
        )
        return await self.create(user)

    async def update(self, user: User) -> User:
        """
        Persist changes made to an already-tracked `User` instance.

        Args:
            user: A `User` instance retrieved from this session (via one
                  of the `get_by_*` methods) with attributes already
                  mutated by the caller.

        Returns:
            The updated `User` instance, refreshed with the latest
            database state (e.g., updated `updated_at` timestamp).

        Notes:
            - Since `user` is expected to already be attached to the
              session (SQLAlchemy's unit-of-work tracks attribute
              changes), no explicit `session.add()` call is required.
              It is included defensively in case the instance became
              detached.
        """
        self._session.add(user)
        await self._session.commit()
        await self._session.refresh(user)
        return user

    async def delete(self, user: User) -> None:
        """
        Delete a user record from the database.

        Args:
            user: The `User` instance to delete. Must be attached to the
                  current session (typically retrieved via a `get_by_*`
                  method beforehand).

        Returns:
            None.
        """
        await self._session.delete(user)
        await self._session.commit()

    async def delete_by_id(self, user_id: int) -> bool:
        """
        Delete a user record by ID without requiring a prior fetch.

        Args:
            user_id: The internal integer ID of the user to delete.

        Returns:
            True if a row was deleted, False if no matching user existed.

        Notes:
            - Provided as a lower-overhead alternative to `delete()` when
              the caller only has the ID and does not need the loaded
              entity for any other purpose.
        """
        stmt = sa_delete(User).where(User.id == user_id)
        result = await self._session.execute(stmt)
        await self._session.commit()
        return result.rowcount > 0


__all__ = ["UserRepository"]