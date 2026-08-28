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

import logging
from functools import lru_cache
from typing import List, Literal, Optional

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Strongly-typed application settings.

    Every attribute maps to an environment variable of the same
    name (case-insensitive). Values are loaded from the process
    environment first, falling back to the `.env` file specified
    in `model_config`.
    """

class Settings(BaseSettings):

    # ------------------------------------------------------------------
    # General application metadata
    # ------------------------------------------------------------------
    PROJECT_NAME: str = "Enterprise Real Estate AI Copilot CRM"
    VERSION: str = "1.0.0"
    API_V1_PREFIX: str = "/api/v1"

    ENVIRONMENT: Literal["development", "staging", "production", "testing"] = Field(
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
    SECRET_KEY: SecretStr = Field(
        default=SecretStr("CHANGE_ME_IN_PRODUCTION"),
        description="Secret key used to sign JWT access/refresh tokens.",
    )
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ------------------------------------------------------------------
    # Database (PostgreSQL)
    # ------------------------------------------------------------------
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: SecretStr = SecretStr("postgres")
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "real_estate_crm"

    # Fully assembled SQLAlchemy database URL. Built automatically
    # from the discrete POSTGRES_* fields above if not explicitly set.
    DATABASE_URL: Optional[str] = None

    # Optional override for the isolated database used by the test
    # suite (e.g. tests/test_settings_api.py). When unset, tests fall
    # back to deriving a DSN from the POSTGRES_* fields above, so this
    # is only needed if the test DB lives on a different host/user.
    TEST_DATABASE_URL: Optional[str] = None

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
    # Accepts a plain comma-separated string from the environment
    # (e.g. "http://localhost:3000,http://x.com") and splits it
    # ourselves in the validator below. Native list values (e.g.
    # defaults or values set programmatically in tests) pass through.
    # NOTE: kept as a plain `str` (not `List[str]`) because pydantic-settings
    # attempts a JSON-decode of any complex-typed field read from the
    # environment/.env *before* field validators run, which breaks on the
    # plain comma-separated format documented in .env.example.txt (e.g.
    # "http://localhost:3000,http://localhost:5173"). Use the
    # `cors_origins` property below to get the parsed list.
    BACKEND_CORS_ORIGINS: str = ""

    @property
    def cors_origins(self) -> List[str]:
        """Parsed list of allowed CORS origins from BACKEND_CORS_ORIGINS."""
        return [origin.strip() for origin in self.BACKEND_CORS_ORIGINS.split(",") if origin.strip()]

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
        password = (
            password.get_secret_value()
            if isinstance(password, SecretStr)
            else password
        )
        server = data.get("POSTGRES_SERVER")
        port = data.get("POSTGRES_PORT")
        db = data.get("POSTGRES_DB")

        return f"postgresql+psycopg://{user}:{password}@{server}:{port}/{db}"

    # ------------------------------------------------------------------
    # AI Copilot / Embeddings
    # ------------------------------------------------------------------
    ANTHROPIC_API_KEY: SecretStr = Field(
        default=SecretStr(""),
        description="API key for the Anthropic provider used by AIProviderClient.",
    )
    AI_REQUEST_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        description="Timeout, in seconds, for outbound AI/embedding provider HTTP calls.",
    )
    AI_DOCUMENT_STORAGE_PATH: str = Field(
        default="./storage/knowledge_documents",
        description="Local filesystem directory where uploaded knowledge documents are stored.",
    )

    EMBEDDING_API_KEY: SecretStr = Field(
        default=SecretStr(""),
        description="API key for the embedding provider used by EmbeddingService.",
    )
    EMBEDDING_API_BASE_URL: str = Field(
        default="https://api.openai.com/v1",
        description="Base URL of the embedding provider's API.",
    )
    EMBEDDING_MODEL: str = Field(
        default="text-embedding-3-small",
        description="Name of the embedding model to request from the provider.",
    )
    EMBEDDING_DIMENSIONS: int = Field(
        default=1536,
        gt=0,
        description="Dimensionality of generated embedding vectors. Must match the "
        "pgvector column dimension configured on the `embeddings` table.",
    )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    LOG_LEVEL: str = "INFO"

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Fail fast at startup if LOG_LEVEL is not a recognized
        Python logging level, rather than failing later on first
        log call.
        """
        v = v.upper()
        valid_levels = logging._nameToLevel.keys()  # DEBUG, INFO, WARNING, ERROR, CRITICAL
        if v not in valid_levels:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(valid_levels)}, got {v!r}"
            )
        return v

    # ------------------------------------------------------------------
    # Cross-field production safety checks
    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def enforce_production_safety(self) -> "Settings":
        """Fail fast at startup rather than silently running an
        insecure configuration in production.
        """
        if self.ENVIRONMENT == "production":
            if self.SECRET_KEY.get_secret_value() == "CHANGE_ME_IN_PRODUCTION":
                raise ValueError(
                    "SECRET_KEY must be overridden via environment/.env in production."
                )
            if self.DEBUG:
                raise ValueError("DEBUG must be False in production.")
            if not self.cors_origins or "*" in self.cors_origins:
                raise ValueError(
                    "BACKEND_CORS_ORIGINS must be an explicit, non-wildcard list in production."
                )
        return self

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