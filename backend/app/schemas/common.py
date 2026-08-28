"""Common, reusable schemas shared across the application's API modules.

Currently contains generic pagination envelope(s) used by list endpoints
across the AI Copilot module and any other module that returns paginated
collections.
"""

from __future__ import annotations

import math
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated collection envelope.

    Pydantic v2 note: unlike Pydantic v1 (which required subclassing
    `pydantic.generics.GenericModel`), Pydantic v2 supports generics
    natively — a plain `BaseModel` combined with `Generic[T]` is
    sufficient. `pydantic.generics` does not exist in v2 and importing it
    raises an ImportError.

    Attributes:
        items: The page of items being returned.
        total: Total number of items across all pages.
        page: The current 1-indexed page number.
        page_size: Maximum number of items per page.
        total_pages: Total number of pages available, derived from
            `total` and `page_size` if not explicitly supplied.
    """

    model_config = ConfigDict(from_attributes=True)

    items: list[T]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_pages: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def compute_total_pages(self) -> "PaginatedResponse[T]":
        """Derive total_pages from total/page_size when not explicitly set.

        Returns:
            The PaginatedResponse instance with total_pages populated.
        """
        if self.total_pages == 0 and self.total > 0:
            self.total_pages = math.ceil(self.total / self.page_size)
        return self