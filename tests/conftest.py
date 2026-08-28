"""
Shared pytest fixtures for the Enterprise Real Estate AI Copilot CRM test suite.

This module enforces isolated, environment-based test configuration and
provides reusable fixtures for mocking the database session, external AI
services, and external email/notification services so that no unit test
ever touches a production system.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
# Ensure the project root (parent of tests/) is importable so `backend.*`
# modules can be resolved regardless of how pytest is invoked.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# ---------------------------------------------------------------------------
# Isolated, environment-based test configuration.
# These MUST be set before any `backend` module is imported, since backend
# configuration is typically read from the environment at import time.
# Using setdefault avoids clobbering any values explicitly provided by CI.
# ---------------------------------------------------------------------------
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-in-prod")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-do-not-use-in-prod")
os.environ.setdefault("AI_SERVICE_API_KEY", "test-ai-service-key")
os.environ.setdefault("AI_SERVICE_BASE_URL", "https://mock-ai.local")
os.environ.setdefault("EMAIL_SERVICE_API_KEY", "test-email-service-key")
os.environ.setdefault("EMAIL_SERVICE_BASE_URL", "https://mock-email.local")
os.environ.setdefault("ALLOW_PRODUCTION_DB_WRITES", "false")

from backend.models import Lead, LeadStatus, Property, PropertyStatus  # noqa: E402
from backend.repositories import LeadRepository, PropertyRepository  # noqa: E402
from backend.services import LeadService, PropertyService  # noqa: E402


# ---------------------------------------------------------------------------
# Event loop (session scoped so async fixtures/tests share one loop)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Safety guard: fail loudly if a test ever tries to reach a real DATABASE_URL
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _guard_against_production_database() -> None:
    db_url = os.environ.get("DATABASE_URL", "")
    if "memory" not in db_url and "test" not in db_url:
        pytest.fail(
            "Refusing to run unit tests against a non-test DATABASE_URL. "
            "Set DATABASE_URL to an in-memory or test-only database."
        )


# ---------------------------------------------------------------------------
# Mock infrastructure fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_db_session() -> AsyncMock:
    """A fully mocked async DB session; no real database is ever touched."""
    session = AsyncMock()
    session.add = MagicMock()
    session.delete = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.refresh = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def mock_ai_service() -> AsyncMock:
    """Mock for the external AI copilot/recommendation service."""
    service = AsyncMock()
    service.score_lead = AsyncMock(return_value=78)
    service.recommend_properties = AsyncMock(return_value=[])
    service.generate_summary = AsyncMock(return_value="AI generated summary")
    return service


@pytest.fixture
def mock_email_service() -> AsyncMock:
    """Mock for the external transactional email/notification service."""
    service = AsyncMock()
    service.send_email = AsyncMock(return_value=True)
    service.send_welcome_email = AsyncMock(return_value=True)
    return service


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_lead_data() -> dict:
    return {
        "id": "lead-1001",
        "name": "Alex Rivera",
        "email": "alex.rivera@example.com",
        "phone": "+14155552671",
        "status": LeadStatus.NEW,
        "score": 0,
        "source": "website",
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


@pytest.fixture
def sample_property_data() -> dict:
    return {
        "id": "prop-2001",
        "address": "500 Market Street, San Francisco, CA",
        "price": Decimal("1250000.00"),
        "bedrooms": 3,
        "bathrooms": 2,
        "square_feet": 1800,
        "status": PropertyStatus.ACTIVE,
        "listing_date": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


@pytest.fixture
def sample_lead(sample_lead_data: dict) -> Lead:
    return Lead(**sample_lead_data)


@pytest.fixture
def sample_property(sample_property_data: dict) -> Property:
    return Property(**sample_property_data)


# ---------------------------------------------------------------------------
# Repository fixtures (backed entirely by mock_db_session)
# ---------------------------------------------------------------------------
@pytest.fixture
def lead_repository(mock_db_session: AsyncMock) -> LeadRepository:
    return LeadRepository(session=mock_db_session)


@pytest.fixture
def property_repository(mock_db_session: AsyncMock) -> PropertyRepository:
    return PropertyRepository(session=mock_db_session)


# ---------------------------------------------------------------------------
# Service fixtures (backed by repositories + mocked external services)
# ---------------------------------------------------------------------------
@pytest.fixture
def lead_service(
    lead_repository: LeadRepository,
    mock_ai_service: AsyncMock,
    mock_email_service: AsyncMock,
) -> LeadService:
    return LeadService(
        repository=lead_repository,
        ai_service=mock_ai_service,
        email_service=mock_email_service,
    )


@pytest.fixture
def property_service(
    property_repository: PropertyRepository,
    mock_ai_service: AsyncMock,
) -> PropertyService:
    return PropertyService(repository=property_repository, ai_service=mock_ai_service)