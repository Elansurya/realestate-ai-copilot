"""
backend/app/models/user.py

SQLAlchemy 2.x ORM model representing an application user (staff member)
within the Real Estate AI Copilot CRM.

Design Notes:
    - This model intentionally excludes relationships (e.g., to leads,
      properties, deals) to keep Phase 03 scoped strictly to
      Authentication & Authorization. Relationships will be introduced
      in later phases once dependent models exist.
    - `role` uses a native Python Enum mapped to a Postgres ENUM type for
      strong data integrity at the database level (invalid role strings
      cannot be inserted, even via raw SQL).
    - All timestamps are timezone-aware (UTC) to avoid ambiguity across
      deployments/regions — a mandatory practice for enterprise SaaS.
    - `uuid` is stored as a String (not native PG UUID) to keep the model
      portable across database backends and simplify serialization;
      value generation is handled at the application/service layer.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


# --------------------------------------------------------------------------
# Role Enumeration
# --------------------------------------------------------------------------
class UserRole(str, enum.Enum):
    """
    Defines the set of permissible roles for a CRM user.

    Inherits from `str` so that:
        - Values serialize cleanly to JSON without custom encoders.
        - Comparisons (e.g., `user.role == UserRole.ADMIN`) work naturally.
        - The underlying database ENUM stores human-readable labels.
    """

    ADMIN = "ADMIN"
    SALES_MANAGER = "SALES_MANAGER"
    SALES_AGENT = "SALES_AGENT"


# --------------------------------------------------------------------------
# User Model
# --------------------------------------------------------------------------
class User(Base):
    """
    Represents an internal CRM user (admin, sales manager, or sales agent).

    This model is the authoritative identity record used for:
        - Authentication (password hash verification)
        - Authorization (role-based access control)
        - Auditing (created_at / updated_at trail)

    Table: users
    """

    __tablename__ = "users"

    # ----------------------------------------------------------------
    # Primary Key
    # ----------------------------------------------------------------
    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        doc="Internal surrogate primary key.",
    )

    # ----------------------------------------------------------------
    # Public Identifier
    # ----------------------------------------------------------------
    uuid: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        nullable=False,
        index=True,
        doc=(
            "Globally unique, non-sequential public identifier exposed "
            "via API responses in place of the internal `id`, preventing "
            "enumeration attacks and ID leakage."
        ),
    )

    # ----------------------------------------------------------------
    # Profile Fields
    # ----------------------------------------------------------------
    full_name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
        doc="User's full display name.",
    )

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        doc="Unique email address; primary login identifier.",
    )

    phone: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        nullable=False,
        doc="Unique phone number; used for contact and optional OTP flows.",
    )

    # ----------------------------------------------------------------
    # Security Fields
    # ----------------------------------------------------------------
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        doc=(
            "Bcrypt password hash (see app.core.security.hash_password). "
            "Plaintext passwords are never persisted."
        ),
    )

    # ----------------------------------------------------------------
    # Authorization Field
    # ----------------------------------------------------------------
    role: Mapped[UserRole] = mapped_column(
        SAEnum(
            UserRole,
            name="user_role",
            native_enum=True,
            validate_strings=True,
        ),
        nullable=False,
        default=UserRole.SALES_AGENT,
        server_default=UserRole.SALES_AGENT.value,
        index=True,
        doc="Role governing RBAC permissions within the CRM.",
    )

    # ----------------------------------------------------------------
    # Account Status Flags
    # ----------------------------------------------------------------
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        doc=(
            "Soft-disable flag. Inactive users must be denied "
            "authentication even with valid credentials."
        ),
    )

    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        doc=(
            "Indicates whether the user has verified their email/phone. "
            "Unverified users may be restricted to limited access."
        ),
    )

    # ----------------------------------------------------------------
    # Audit Timestamps (Timezone-Aware, UTC)
    # ----------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        doc="UTC timestamp when the user record was created.",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        doc="UTC timestamp when the user record was last updated.",
    )

    # ----------------------------------------------------------------
    # Developer Ergonomics
    # ----------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"<User id={self.id} uuid={self.uuid!r} email={self.email!r} "
            f"role={self.role.value} is_active={self.is_active}>"
        )