"""
Application configuration module.

Centralizes all environment-driven settings using Pydantic's
`BaseSettings`. This ensures:
  - A single source of truth for configuration values.
  - Automatic type validation/coercion of environment variables.
  - Easy overriding per environment (.env, Docker, CI/CD, etc.)
  - No hardcoded secrets/config scattered across the codebase.

Usage:
    from app.core.config import settings
    settings.DATABASE_URL
"""

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Annotated


class Settings(BaseSettings):
    """
    Strongly-typed application settings.

    Every attribute maps to an environment variable of the same
    name (case-insensitive). Values are loaded from the process
    environment first, falling back to the `.env` file specified
    in `model_config`.
    """

    # ------------------------------------------------------------------
    # General application metadata
    # ------------------------------------------------------------------
    PROJECT_NAME: str = "Enterprise Real Estate AI Copilot CRM"
    API_V1_PREFIX: str = "/api/v1"
    ENVIRONMENT: str = Field(
        default="development",
        description="One of: development, staging, production, testing",
    )
    DEBUG: bool = False

    # ------------------------------------------------------------------
    # Server
    # ------------------------------------------------------------------
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # ------------------------------------------------------------------
    # Security / JWT (values used starting Phase 02+ auth implementation,
    # declared here now so config is centralized from day one)
    # ------------------------------------------------------------------
    SECRET_KEY: str = Field(
        default="CHANGE_ME_IN_PRODUCTION",
        description="Secret key used to sign JWT access/refresh tokens.",
    )
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ------------------------------------------------------------------
    # Database (PostgreSQL)
    # ------------------------------------------------------------------
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "real_estate_crm"

    # Fully assembled SQLAlchemy database URL. Built automatically
    # from the discrete POSTGRES_* fields above if not explicitly set.
    DATABASE_URL: Optional[str] = None

    # Toggle SQL statement echoing for local debugging only.
    DB_ECHO: bool = False

    # Connection pool tuning (sane enterprise defaults).
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800  # seconds

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    # `NoDecode` tells pydantic-settings NOT to attempt its default
    # JSON decoding for this list field, so we can accept a plain
    # comma-separated string from the environment (e.g.
    # "http://localhost:3000,http://x.com") and split it ourselves
    # in the validator below.
    BACKEND_CORS_ORIGINS: List[str] = []

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v):
        """Splits a comma-separated CORS origins string from the
        environment into a list. Native list values (e.g. defaults
        or values set programmatically in tests) pass through as-is.
        """
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def assemble_db_url(cls, v: Optional[str], info) -> str:
        """Build the PostgreSQL DSN from discrete fields when
        DATABASE_URL is not explicitly supplied via environment.
        """
        if isinstance(v, str) and v:
            return v

        data = info.data
        user = data.get("POSTGRES_USER")
        password = data.get("POSTGRES_PASSWORD")
        server = data.get("POSTGRES_SERVER")
        port = data.get("POSTGRES_PORT")
        db = data.get("POSTGRES_DB")

        return f"postgresql+psycopg://{user}:{password}@{server}:{port}/{db}"

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """
    Returns a cached singleton instance of Settings.

    `lru_cache` ensures the environment/`.env` file is parsed only
    once per process, which avoids repeated disk/env reads and
    guarantees every part of the app shares identical configuration.
    """
    return Settings()


# Module-level singleton for convenient importing:
#   from app.core.config import settings
settings = get_settings()