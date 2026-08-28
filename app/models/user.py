"""
backend/app/models/user.py

SQLAlchemy 2.x ORM model representing an application user (staff member)
within the Real Estate AI Copilot CRM.

Design Notes:
    - `assigned_properties` is the inverse side of `Property.assigned_agent`
      (a one-to-many: one User manages many Property rows). It is declared
      here as the mandatory mirror of that relationship's `back_populates`
      contract — SQLAlchemy raises `InvalidRequestError` at mapper
      configuration time if a `back_populates` target is missing on the
      other model, which is why this attribute must exist even though
      `User` otherwise stays scoped to auth concerns. `Property` is
      imported only under `TYPE_CHECKING` to avoid a runtime circular
      import (`property.py` depends on `user.py` already being loadable).
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
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.audit_log import AuditLog
    from app.models.property import Property


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
        default=lambda: str(uuid.uuid4()),
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
    # Relationships
    # ----------------------------------------------------------------
    assigned_properties: Mapped[List["Property"]] = relationship(
        "Property",
        back_populates="assigned_agent",
        foreign_keys="[Property.assigned_agent_id]",
        lazy="raise_on_sql",
        passive_deletes=True,
        doc=(
            "All Property listings currently assigned to this User as "
            "agent. Mirrors `Property.assigned_agent`; required because "
            "that relationship declares `back_populates="
            "'assigned_properties'`.\n\n"
            "`lazy='raise_on_sql'` is deliberate, not an oversight: "
            "`User` is loaded on essentially every authenticated request "
            "via `get_current_user()` -> `UserRepository.get_by_id()`, "
            "none of which ever touch this collection today. Defaulting "
            "it to eager (`selectin`) would silently add an extra query "
            "— potentially returning a large row set — to every single "
            "authenticated API call. `raise_on_sql` makes that path "
            "cheap by default and forces any future caller that actually "
            "needs a user's assigned properties to request it explicitly "
            "(e.g. `select(User).options(selectinload(User."
            "assigned_properties))`), which also surfaces as a clear, "
            "immediate error instead of a silent implicit query or an "
            "async `MissingGreenlet` failure if triggered outside a live "
            "session context.\n\n"
            "`passive_deletes=True` is required as a consequence: "
            "`Property.assigned_agent_id` already declares "
            "`ondelete='SET NULL'` at the FK level, so on `User` deletion "
            "(see `UserRepository.delete()`, which uses `AsyncSession."
            "delete()`) this tells the ORM to trust Postgres to null out "
            "child rows via that constraint, rather than trying to load "
            "this collection into memory to manage it row-by-row — which "
            "would otherwise conflict with `lazy='raise_on_sql'` and "
            "raise on the very delete path that must always succeed."
        ),
    )

    audit_logs: Mapped[List["AuditLog"]] = relationship(
        "AuditLog",
        back_populates="user",
        foreign_keys="[AuditLog.user_id]",
        lazy="raise_on_sql",
        doc=(
            "All AuditLog entries recorded against this User. "
            "Mirrors `AuditLog.user`; required because that relationship "
            "declares `back_populates='audit_logs'`. `lazy='raise_on_sql'` "
            "for the same reasons as `assigned_properties` above."
        ),
    )

    # ----------------------------------------------------------------
    # Developer Ergonomics
    # ----------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"<User id={self.id} uuid={self.uuid!r} email={self.email!r} "
            f"role={self.role.value} is_active={self.is_active}>"
        )