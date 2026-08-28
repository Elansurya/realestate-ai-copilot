"""Pydantic schemas for reusable, versioned AI prompt templates."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.prompt_template import PromptCategory


class PromptVariableDefinition(BaseModel):
    """Definition of a single expected variable within a prompt template.

    Attributes:
        name: Name of the variable as referenced in the template text.
        description: Human readable description of the variable's purpose.
        required: Whether the variable must be supplied when rendering.
        default: Default value used when the variable is not supplied.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    required: bool = True
    default: Optional[str] = None


class PromptTemplateCreate(BaseModel):
    """Payload for creating a new prompt template.

    Attributes:
        name: Machine-readable name of the template.
        description: Human readable description of the template's purpose.
        template_text: Raw prompt text containing variable placeholders.
        category: Functional category the template belongs to.
        variables: List of variable definitions expected by the template.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(min_length=1, max_length=150)
    description: Optional[str] = Field(default=None, max_length=500)
    template_text: str = Field(min_length=1)
    category: PromptCategory
    variables: list[PromptVariableDefinition] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Normalize the template name to a consistent lowercase format.

        Args:
            value: Raw template name supplied by the caller.

        Returns:
            The stripped, lowercased template name.

        Raises:
            ValueError: If the name is blank after stripping.
        """
        stripped = value.strip().lower()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @model_validator(mode="after")
    def validate_variables_referenced_in_template(self) -> "PromptTemplateCreate":
        """Ensure every declared variable appears as a placeholder in the template.

        Returns:
            The validated PromptTemplateCreate instance.

        Raises:
            ValueError: If a declared variable is not referenced in template_text.
        """
        for variable in self.variables:
            placeholder = "{" + variable.name + "}"
            if placeholder not in self.template_text:
                raise ValueError(
                    f"variable '{variable.name}' is declared but not referenced "
                    f"in template_text as '{placeholder}'"
                )
        return self


class PromptTemplateUpdate(BaseModel):
    """Payload for partially updating an existing prompt template.

    Attributes:
        description: Updated description, if provided.
        template_text: Updated raw prompt text, if provided.
        variables: Updated list of variable definitions, if provided.
        is_active: Updated activation flag, if provided.
    """

    model_config = ConfigDict(from_attributes=True)

    description: Optional[str] = Field(default=None, max_length=500)
    template_text: Optional[str] = Field(default=None, min_length=1)
    variables: Optional[list[PromptVariableDefinition]] = None
    is_active: Optional[bool] = None


class PromptTemplateRead(BaseModel):
    """Representation of a persisted prompt template returned to API consumers.

    Attributes:
        id: Unique identifier of the prompt template.
        name: Machine-readable name of the template.
        description: Human readable description of the template's purpose.
        template_text: Raw prompt text containing variable placeholders.
        category: Functional category the template belongs to.
        version: Version number of this template revision.
        variables: List of variable definitions expected by the template.
        is_active: Whether this template version is currently usable.
        created_at: Timestamp of template creation.
        updated_at: Timestamp of last template update.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: Optional[str] = None
    template_text: str
    category: PromptCategory
    version: int
    variables: list[PromptVariableDefinition] = Field(default_factory=list)
    is_active: bool
    created_at: datetime
    updated_at: datetime


class PromptRenderRequest(BaseModel):
    """Request payload for rendering a prompt template with concrete values.

    Attributes:
        template_id: Identifier of the prompt template to render.
        variable_values: Mapping of variable names to concrete substitution values.
    """

    model_config = ConfigDict(from_attributes=True)

    template_id: UUID
    variable_values: dict[str, str] = Field(default_factory=dict)


class PromptRenderResponse(BaseModel):
    """Response payload containing a fully rendered prompt.

    Attributes:
        template_id: Identifier of the prompt template that was rendered.
        rendered_text: Final prompt text with all variables substituted.
    """

    model_config = ConfigDict(from_attributes=True)

    template_id: UUID
    rendered_text: str