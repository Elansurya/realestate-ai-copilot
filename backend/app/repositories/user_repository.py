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
    - `list_users()` and `count_users()` (Milestone 4: User Management
      Module) share a single private filter-building helper
      (`_apply_filters`) so that "how many users match X" and "which
      users match X" can never silently drift apart as filters evolve.
      `list_users()`'s original `skip`/`limit` positional signature is
      preserved unchanged; the new `role`, `is_active`, and `search`
      filters are added as keyword-only parameters with `None` defaults,
      so every existing call site remains valid without modification.
    - `create()` rolls back and re-raises on any write failure (e.g. a
      unique-constraint violation surfaced as `IntegrityError`), logging
      the exception via the standard `logging` module rather than
      `print`/`traceback.print_exc()` so failures are captured by the
      application's configured log handlers/aggregators instead of only
      appearing on stdout.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.models.user import User, UserRole

logger = logging.getLogger(__name__)


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

    async def list_users(
        self,
        skip: int = 0,
        limit: int = 100,
        *,
        role: Optional[UserRole] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> Sequence[User]:
        """
        Retrieve a paginated list of users, ordered by primary key,
        optionally filtered by role, active status, and/or a free-text
        search term.

        Args:
            skip: Number of records to skip (offset), for pagination.
            limit: Maximum number of records to return.
            role: If provided, restrict results to users with exactly
                  this `UserRole`. Omit (`None`) to match any role.
            is_active: If provided, restrict results to users whose
                       `is_active` flag equals this value. Omit (`None`)
                       to match both active and inactive users.
            search: If provided, restrict results to users whose
                    `full_name` or `email` contains this substring
                    (case-insensitive). Omit (`None`) to skip text
                    filtering.

        Returns:
            A sequence of `User` instances matching the given filters
            (may be empty).

        Notes:
            - `skip` and `limit` remain positional, matching the
              original signature exactly. `role`, `is_active`, and
              `search` are keyword-only additions, so all pre-existing
              call sites continue to work without modification.
        """
        stmt: Select = select(User)
        stmt = self._apply_filters(stmt, role=role, is_active=is_active, search=search)
        stmt = stmt.order_by(User.id).offset(skip).limit(limit)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def count_users(
        self,
        *,
        role: Optional[UserRole] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> int:
        """
        Count the total number of users matching the given filters,
        independent of pagination.

        Intended to be called alongside `list_users()` with identical
        filter arguments, to populate the `total` field of a paginated
        response envelope (e.g., `PaginatedUserResponse`).

        Args:
            role: If provided, restrict the count to users with exactly
                  this `UserRole`. Omit (`None`) to match any role.
            is_active: If provided, restrict the count to users whose
                       `is_active` flag equals this value. Omit (`None`)
                       to match both active and inactive users.
            search: If provided, restrict the count to users whose
                    `full_name` or `email` contains this substring
                    (case-insensitive). Omit (`None`) to skip text
                    filtering.

        Returns:
            The total count of matching `User` records as an integer.
        """
        stmt = select(func.count()).select_from(User)
        stmt = self._apply_filters(stmt, role=role, is_active=is_active, search=search)
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    @staticmethod
    def _apply_filters(
        stmt: Select,
        *,
        role: Optional[UserRole],
        is_active: Optional[bool],
        search: Optional[str],
    ) -> Select:
        """
        Apply the shared set of optional `WHERE` filters to a SQLAlchemy
        `Select` statement targeting the `User` model.

        Centralizing this logic ensures `list_users()` and
        `count_users()` can never apply inconsistent filter semantics
        (e.g., one matching case-sensitively and the other not), since
        both delegate to this single implementation.

        Args:
            stmt: The base `Select` statement to augment (either a
                  `select(User)` or a `select(func.count()).select_from(User)`).
            role: Optional exact-match filter on `User.role`.
            is_active: Optional exact-match filter on `User.is_active`.
            search: Optional case-insensitive substring filter matched
                    against `User.full_name` or `User.email`.

        Returns:
            The same `Select` statement with the applicable `WHERE`
            clauses appended.
        """
        if role is not None:
            stmt = stmt.where(User.role == role)

        if is_active is not None:
            stmt = stmt.where(User.is_active == is_active)

        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                or_(
                    User.full_name.ilike(pattern),
                    User.email.ilike(pattern),
                )
            )

        return stmt

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

        Raises:
            Exception: Re-raises whatever the underlying database
                operation raised (e.g. `sqlalchemy.exc.IntegrityError`
                on a unique-constraint violation), after rolling back
                the session and logging the failure. Callers (typically
                `AuthService.register`) are expected to catch specific
                exception types (e.g. `IntegrityError`) to translate
                them into appropriate HTTP responses.
        """
        try:
            self._session.add(user)
            await self._session.commit()
            await self._session.refresh(user)
        except Exception:
            await self._session.rollback()
            logger.exception(
                "Failed to create user (email=%s, phone=%s); rolled back.",
                user.email,
                user.phone,
            )
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