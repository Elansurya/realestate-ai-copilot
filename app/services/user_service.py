"""
backend/app/services/user_service.py

Service layer for the User Management Module (Milestone 4).

Responsibilities:
    - Own all business rules for self-service profile management
      (viewing/updating one's own profile, changing one's own password)
      and admin-driven user administration (listing, viewing, updating
      role/status, deactivating/reactivating other users).
    - Compose `UserRepository` calls to implement these use cases;
      contains NO direct database queries itself.
    - Translate business-rule violations (e.g., wrong old password,
      duplicate phone number, user not found) into `HTTPException`s,
      following the same convention already established by
      `AuthService` in this codebase (services raise `HTTPException`
      directly; routers do not need their own error-translation logic).

Design Notes:
    - This service is intentionally SEPARATE from `AuthService`.
      `AuthService` owns identity/token concerns (register, login,
      refresh); `UserService` owns profile and administrative user
      concerns. Mixing the two would violate the Single Responsibility
      Principle and make both classes harder to test and reason about.
    - Self-service methods (`get_profile`, `update_profile`,
      `change_password`) accept the already-authenticated `User`
      instance directly (as resolved by `get_current_user` at the API
      layer) rather than re-deriving identity from a raw ID — the
      caller's identity is not in question for these methods, only the
      requested change.
    - Admin methods (`list_users`, `get_user_by_uuid`, `update_user`,
      `deactivate_user`, `reactivate_user`) operate on a target user
      identified by public `uuid`. Authorization (i.e., confirming the
      *caller* is permitted to invoke these methods at all) is enforced
      at the API layer via `app.dependencies.rbac.require_roles` and is
      NOT re-implemented here — this service trusts that it is only
      reached by already-authorized callers, keeping authorization and
      business logic cleanly separated.
    - `list_users()` returns a raw `(items, total)` tuple rather than a
      `PaginatedUserResponse`. Assembling the transport-facing envelope
      (including `page`/`page_size` presentation fields) is left to the
      API layer, keeping this service free of response-schema concerns.
    - AUDIT LOGGING: every state-mutating method has a clearly marked
      `# --- Audit Hook ---` comment placed immediately after the
      mutation is committed. A future `AuditLogService.record(...)` call
      can be inserted at each marked point without changing any public
      method signature, return type, or the router code that calls it.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from fastapi import HTTPException, status

from app.core.security import hash_password, verify_password
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository
from app.schemas.user import ChangePasswordRequest, UserAdminUpdate, UserUpdate


class UserService:
    """
    Service encapsulating business logic for self-service profile
    management and admin-driven user administration.

    Consumed by the User Management API router (`app/api/v1/users.py`),
    which is responsible only for HTTP transport concerns (request
    parsing, response serialization, dependency injection) and delegates
    all decision-making to this class.
    """

    def __init__(self, user_repository: UserRepository) -> None:
        """
        Args:
            user_repository: The data-access repository for the `User`
                              entity, typically injected via a FastAPI
                              dependency provider bound to the current
                              request's `AsyncSession`.
        """
        self._user_repository = user_repository

    # ==================================================================
    # Self-Service Operations
    # ==================================================================
    async def get_profile(self, current_user: User) -> User:
        """
        Retrieve the authenticated caller's own profile.

        Args:
            current_user: The authenticated `User`, resolved by
                          `get_current_user` at the API layer.

        Returns:
            The caller's own `User` instance, unchanged.

        Notes:
            - `current_user` is already a fully loaded, session-attached
              instance (resolved moments earlier by `get_current_user`
              via the repository), so no additional database round-trip
              is required here. This method exists primarily to give
              the router a single, explicit service-layer call site
              (rather than returning `current_user` directly), keeping
              the door open for future logic (e.g., last-seen tracking)
              without changing the router.
        """
        return current_user

    async def update_profile(self, current_user: User, payload: UserUpdate) -> User:
        """
        Apply a self-service profile update to the authenticated
        caller's own record.

        Args:
            current_user: The authenticated `User` performing the
                          update, resolved by `get_current_user`.
            payload: Validated `UserUpdate` containing the fields to
                     change. Only fields explicitly set by the caller
                     are applied (partial-update / `PATCH` semantics);
                     omitted fields are left untouched.

        Returns:
            The updated `User` instance, refreshed with the latest
            database state.

        Raises:
            HTTPException(409): If `payload.phone` is supplied and
                already belongs to a different user account.

        Notes:
            - `payload` is `UserUpdate`, which structurally excludes
              `role` and `email` — there is no field to accidentally
              apply for either, even if this method's logic were
              modified carelessly in the future.
        """
        update_fields = payload.model_dump(exclude_unset=True)

        if "phone" in update_fields and update_fields["phone"] != current_user.phone:
            existing = await self._user_repository.get_by_phone(update_fields["phone"])
            if existing is not None and existing.id != current_user.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This phone number is already registered to another account.",
                )

        for field_name, value in update_fields.items():
            setattr(current_user, field_name, value)

        updated_user = await self._user_repository.update(current_user)

        # --- Audit Hook ---
        # await audit_log_service.record(
        #     actor_id=updated_user.id,
        #     action="PROFILE_UPDATED",
        #     target_type="User",
        #     target_id=updated_user.id,
        #     changes=update_fields,
        # )

        return updated_user

    async def change_password(
        self,
        current_user: User,
        payload: ChangePasswordRequest,
    ) -> User:
        """
        Change the authenticated caller's own password, after verifying
        their current password.

        Args:
            current_user: The authenticated `User` changing their
                          password, resolved by `get_current_user`.
            payload: Validated `ChangePasswordRequest` containing the
                     current plaintext password (for re-verification)
                     and the new plaintext password.

        Returns:
            The updated `User` instance, with `password_hash` replaced
            by the hash of the new password.

        Raises:
            HTTPException(400): If `payload.old_password` does not
                match the caller's currently stored password hash.
        """
        if not verify_password(payload.old_password, current_user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The current password provided is incorrect.",
            )

        current_user.password_hash = hash_password(payload.new_password)
        updated_user = await self._user_repository.update(current_user)

        # --- Audit Hook ---
        # await audit_log_service.record(
        #     actor_id=updated_user.id,
        #     action="PASSWORD_CHANGED",
        #     target_type="User",
        #     target_id=updated_user.id,
        # )

        return updated_user

    # ==================================================================
    # Admin Operations
    # ==================================================================
    async def list_users(
        self,
        skip: int = 0,
        limit: int = 100,
        *,
        role: Optional[UserRole] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
    ) -> Tuple[Sequence[User], int]:
        """
        Retrieve a paginated, optionally filtered list of all users, for
        admin consumption.

        Args:
            skip: Number of records to skip (offset), for pagination.
            limit: Maximum number of records to return.
            role: Optional exact-match filter on `UserRole`.
            is_active: Optional exact-match filter on active status.
            search: Optional case-insensitive substring filter matched
                    against full name or email.

        Returns:
            A tuple of `(items, total)`, where `items` is the current
            page of matching `User` instances and `total` is the total
            count of matching records across all pages. The API layer
            is responsible for assembling these into a
            `PaginatedUserResponse`.
        """
        items = await self._user_repository.list_users(
            skip=skip,
            limit=limit,
            role=role,
            is_active=is_active,
            search=search,
        )
        total = await self._user_repository.count_users(
            role=role,
            is_active=is_active,
            search=search,
        )
        return items, total

    async def get_user_by_uuid(self, user_uuid: str) -> User:
        """
        Retrieve a single user by their public UUID, for admin
        consumption.

        Args:
            user_uuid: The public UUID of the target user.

        Returns:
            The matching `User` instance.

        Raises:
            HTTPException(404): If no user exists with the given UUID.
        """
        user = await self._user_repository.get_by_uuid(user_uuid)
        if user is None:
            raise self._user_not_found(user_uuid)
        return user

    async def update_user(self, user_uuid: str, payload: UserAdminUpdate) -> User:
        """
        Apply an admin-driven update (role and/or account status) to a
        target user's record.

        Args:
            user_uuid: The public UUID of the target user to update.
            payload: Validated `UserAdminUpdate` containing the fields
                     to change. Only fields explicitly set by the
                     caller are applied (partial-update semantics).

        Returns:
            The updated `User` instance, refreshed with the latest
            database state.

        Raises:
            HTTPException(404): If no user exists with the given UUID.

        Notes:
            - `payload` is `UserAdminUpdate`, which structurally
              excludes `email`, `phone`, and `password_hash` — this
              method can never be used to alter credentials or contact
              details, regardless of caller intent.
        """
        user = await self._user_repository.get_by_uuid(user_uuid)
        if user is None:
            raise self._user_not_found(user_uuid)

        update_fields = payload.model_dump(exclude_unset=True)
        for field_name, value in update_fields.items():
            setattr(user, field_name, value)

        updated_user = await self._user_repository.update(user)

        # --- Audit Hook ---
        # await audit_log_service.record(
        #     actor_id=acting_admin_id,
        #     action="USER_ADMIN_UPDATED",
        #     target_type="User",
        #     target_id=updated_user.id,
        #     changes=update_fields,
        # )

        return updated_user

    async def deactivate_user(self, user_uuid: str) -> User:
        """
        Deactivate a target user's account, revoking their ability to
        authenticate.

        Args:
            user_uuid: The public UUID of the target user to deactivate.

        Returns:
            The updated `User` instance, with `is_active` set to
            `False`.

        Raises:
            HTTPException(404): If no user exists with the given UUID.
            HTTPException(409): If the target user is already inactive.
        """
        user = await self._user_repository.get_by_uuid(user_uuid)
        if user is None:
            raise self._user_not_found(user_uuid)

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This user account is already inactive.",
            )

        user.is_active = False
        updated_user = await self._user_repository.update(user)

        # --- Audit Hook ---
        # await audit_log_service.record(
        #     actor_id=acting_admin_id,
        #     action="USER_DEACTIVATED",
        #     target_type="User",
        #     target_id=updated_user.id,
        # )

        return updated_user

    async def reactivate_user(self, user_uuid: str) -> User:
        """
        Reactivate a previously deactivated target user's account,
        restoring their ability to authenticate.

        Args:
            user_uuid: The public UUID of the target user to reactivate.

        Returns:
            The updated `User` instance, with `is_active` set to `True`.

        Raises:
            HTTPException(404): If no user exists with the given UUID.
            HTTPException(409): If the target user is already active.
        """
        user = await self._user_repository.get_by_uuid(user_uuid)
        if user is None:
            raise self._user_not_found(user_uuid)

        if user.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This user account is already active.",
            )

        user.is_active = True
        updated_user = await self._user_repository.update(user)

        # --- Audit Hook ---
        # await audit_log_service.record(
        #     actor_id=acting_admin_id,
        #     action="USER_REACTIVATED",
        #     target_type="User",
        #     target_id=updated_user.id,
        # )

        return updated_user

    # ==================================================================
    # Internal Helpers
    # ==================================================================
    @staticmethod
    def _user_not_found(user_uuid: str) -> HTTPException:
        """
        Build a standardized 404 Not Found exception for an unresolved
        user UUID.

        Args:
            user_uuid: The UUID that failed to resolve to a user.

        Returns:
            A configured `HTTPException` ready to be raised.
        """
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No user found with uuid '{user_uuid}'.",
        )


__all__ = ["UserService"]