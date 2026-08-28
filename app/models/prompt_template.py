"""SQLAlchemy model for reusable, versioned AI prompt templates."""

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PromptCategory(str, enum.Enum):
    """Enumeration of functional categories for prompt templates."""

    CHAT = "chat"
    RAG = "rag"
    SQL_GENERATION = "sql_generation"
    ANALYTICS = "analytics"
    LEAD_SCORING = "lead_scoring"
    PROPERTY_DESCRIPTION = "property_description"
    EMAIL_DRAFTING = "email_drafting"


class PromptTemplate(Base):
    """Represents a versioned, reusable AI prompt template.

    Attributes:
        id: Primary key UUID identifier.
        name: Machine-readable name of the template.
        description: Human readable description of template purpose.
        template_text: The raw prompt text containing variable placeholders.
        category: Functional category the template belongs to.
        version: Monotonically increasing version number for the template name.
        variables: JSONB list of variable names expected by the template.
        is_active: Whether this template version is currently usable.
        created_by: Foreign key referencing the CRM user who authored it.
        is_deleted: Soft delete flag.
        deleted_at: Timestamp of soft deletion, if any.
        created_at: Timestamp of record creation.
        updated_at: Timestamp of last record update.
    """

    __tablename__ = "prompt_templates"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_prompt_templates_name_version"),
        Index("ix_prompt_templates_category_is_active", "category", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[PromptCategory] = mapped_column(
        SAEnum(PromptCategory, name="prompt_category", native_enum=True), nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    variables: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        """Return a debug-friendly representation of the prompt template."""
        return f"<PromptTemplate id={self.id} name={self.name} version={self.version}>"