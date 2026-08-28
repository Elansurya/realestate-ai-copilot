"""
Shared notification service types.

This module intentionally has no imports from notification_service
or any channel service. It exists to prevent circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class DispatchResult:
    """Result of a notification channel dispatch attempt."""

    success: bool
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None