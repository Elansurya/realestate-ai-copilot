"""
backend/app/api/dependencies/lead.py

Centralized dependency-injection providers for the Lead CRM module.

Responsibilities:
    - Wire together the persistence and business-logic layers for lead
      management, exposing them as FastAPI-injectable dependencies.
    - Define the canonical construction chain:
      AsyncSession -> LeadRepository -> LeadService.

Design Notes:
    - This module contains NO SQL, NO business logic, and NO API route
      definitions. Each provider function is a thin, single-purpose
      factory that FastAPI resolves via `Depends(...)`.
    - `get_lead_repository` and `get_lead_service` are kept as separate,
      independently injectable providers (rather than collapsing them
      into one function) so that either layer can be substituted or
      mocked in isolation during testing, and so that other modules
      needing only a `LeadRepository` (without the service layer) can
      depend on `get_lead_repository` directly.
    - This module establishes the pattern future dependency providers
      for other domains (e.g., `get_user_service`, `get_property_service`,
      `get_booking_service`) should follow: a `get_db`-scoped session,
      a repository provider built from that session, and a service
      provider built from that repository, each as its own small
      function.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.repositories.lead_repository import LeadRepository
from app.services.lead_service import LeadService


def get_lead_repository(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LeadRepository:
    """
    Provide a request-scoped `LeadRepository` instance bound to the
    current database session.

    Args:
        db: An active `AsyncSession`, injected via `get_db`.

    Returns:
        A `LeadRepository` ready to perform data-access operations for
        the current request.
    """
    return LeadRepository(db)


def get_lead_service(
    repository: Annotated[LeadRepository, Depends(get_lead_repository)],
) -> LeadService:
    """
    Provide a request-scoped `LeadService` instance, wired to a
    `LeadRepository` bound to the current database session.

    Args:
        repository: A `LeadRepository` instance, injected via
                    `get_lead_repository`.

    Returns:
        A `LeadService` ready to handle lead-management business logic
        for the current request.
    """
    return LeadService(repository)




__all__ = [
    "get_lead_repository",
    "get_lead_service",
]