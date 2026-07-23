"""
backend/app/api/v1/auth.py

API router for Authentication & Authorization endpoints.

Responsibilities:
    - Expose HTTP endpoints for registration, login, token refresh, and
      current-user profile retrieval.
    - Perform request/response schema validation only.
    - Delegate all business logic (credential verification, token
      issuance/validation, user lookup, user creation) to `AuthService`
      and the authentication dependencies.

Design Notes:
    - This router contains NO direct database queries or password/JWT
      handling — those concerns are fully encapsulated in
      `app.services.auth_service.AuthService` and
      `app.core.security`, keeping this layer thin and testable.
    - `AuthService` is instantiated per-request via a small dependency
      provider (`get_auth_service`) so it can be swapped/mocked in tests
      without modifying route signatures.
    - The `/login` endpoint accepts `OAuth2PasswordRequestForm`
      (application/x-www-form-urlencoded) rather than a JSON body. This
      is required for FastAPI/Swagger UI's built-in "Authorize" button
      to function, since it always submits credentials via the OAuth2
      password flow's form-encoded contract. The form's `username`
      field is treated as the user's email address; `AuthService.login`
      itself is unchanged and still operates on `email`/`password`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth_dependency import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import (
    RefreshTokenRequest,
    RegisterRequest,
    TokenResponse,
)
from app.schemas.user import UserResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


# --------------------------------------------------------------------------
# Service Dependency Provider
# --------------------------------------------------------------------------
def get_auth_service(db: AsyncSession = Depends(get_db)) -> AuthService:
    """
    Provide a request-scoped `AuthService` instance bound to the current
    database session.

    Args:
        db: An active `AsyncSession`, injected via `get_db`.

    Returns:
        A fully constructed `AuthService` ready to handle the request.
    """
    return AuthService(db)


# --------------------------------------------------------------------------
# POST /auth/register
# --------------------------------------------------------------------------
@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new CRM user account",
    description=(
        "Creates a new user account with the supplied profile details "
        "and credentials. Returns the created user's public profile "
        "(never the password hash). Responds with 409 Conflict if the "
        "email or phone number is already registered, or 400 Bad "
        "Request if an invalid role is supplied.\n\n"
        "SECURITY NOTE: this endpoint currently accepts a caller-"
        "supplied `role`. For production use, restrict role selection "
        "(e.g. force SALES_AGENT by default) and introduce a separate "
        "admin-only endpoint for creating elevated-privilege accounts."
    ),
)
async def register(
    payload: RegisterRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> User:
    """
    Register a new user account.

    Args:
        payload: Validated `RegisterRequest` containing the new user's
                 profile and credentials.
        auth_service: Injected `AuthService` handling registration
                      business logic.

    Returns:
        The newly created user's profile, serialized via `UserResponse`.

    Raises:
        HTTPException(400): If the supplied role is invalid.
        HTTPException(409): If the email or phone is already registered.
    """
    return await auth_service.register(
        full_name=payload.full_name,
        email=payload.email,
        phone=payload.phone,
        password=payload.password,
        role=payload.role,
    )


# --------------------------------------------------------------------------
# POST /auth/login
# --------------------------------------------------------------------------
@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate a user and issue access/refresh tokens",
    description=(
        "Validates the supplied email and password credentials, "
        "submitted as OAuth2 password-flow form data (not JSON) so "
        "that Swagger UI's 'Authorize' button works out of the box. "
        "The form's `username` field is used as the user's email "
        "address. On success, returns a short-lived access token and "
        "a long-lived refresh token. On failure (invalid credentials "
        "or inactive account), responds with 401 Unauthorized."
    ),
)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """
    Authenticate a user and issue a new token pair.

    Args:
        form_data: OAuth2 password-flow form data, injected by FastAPI
                   from the `application/x-www-form-urlencoded` request
                   body. `form_data.username` is treated as the user's
                   email address; `form_data.password` is the plaintext
                   password.
        auth_service: Injected `AuthService` handling authentication
                      business logic.

    Returns:
        `TokenResponse` containing the issued access and refresh tokens.

    Raises:
        HTTPException(401): Propagated from `AuthService.login` when
            credentials are invalid or the account is inactive.
    """
    return await auth_service.login(
        email=form_data.username,
        password=form_data.password,
    )


# --------------------------------------------------------------------------
# POST /auth/refresh
# --------------------------------------------------------------------------
@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange a refresh token for a new access token",
    description=(
        "Validates the supplied refresh token and, if valid and "
        "unexpired, issues a new access token. Responds with 401 "
        "Unauthorized if the refresh token is invalid, expired, of the "
        "wrong type, or the associated user is inactive."
    ),
)
async def refresh(
    payload: RefreshTokenRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """
    Issue a new access token from a valid refresh token.

    Args:
        payload: Validated `RefreshTokenRequest` containing the refresh
                 token.
        auth_service: Injected `AuthService` handling token refresh
                      business logic.

    Returns:
        `TokenResponse` containing a newly issued access token (the
        original refresh token is returned unchanged).

    Raises:
        HTTPException(401): Propagated from
            `AuthService.refresh_access_token` for any invalid, expired,
            mistyped token, or inactive-user condition.
    """
    return await auth_service.refresh_access_token(refresh_token=payload.refresh_token)


# --------------------------------------------------------------------------
# GET /auth/me
# --------------------------------------------------------------------------
@router.get(
    "/me",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve the authenticated user's profile",
    description=(
        "Returns the profile of the currently authenticated user, "
        "resolved from the bearer access token supplied in the "
        "`Authorization` header."
    ),
)
async def get_me(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Return the authenticated user's own profile.

    Args:
        current_user: The `User` instance resolved and validated by the
                      `get_current_user` dependency.

    Returns:
        The authenticated user's profile, serialized via `UserResponse`.
    """
    return current_user


__all__ = ["router"]