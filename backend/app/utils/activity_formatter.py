"""
backend/app/utils/activity_formatter.py

Reusable, presentation-layer formatting utilities for the Activity
Timeline module of the Enterprise Real Estate AI Copilot CRM.

This module is intentionally free of any database, request, or
response-model concerns. It accepts plain ``Activity`` ORM instances
(or lightweight duck-typed objects exposing the same attributes) and
returns plain ``dict`` payloads / ``str`` values that the API layer
(``app/api/v1/activity.py``) or the :mod:`app.utils.timeline_builder`
module can embed directly into responses.

Conventions:
    - Every formatter degrades gracefully: missing/optional fields
      never raise, they simply fall back to sensible defaults.
    - Module-specific formatters read ``old_value`` / ``new_value`` /
      ``meta_data`` defensively (``dict`` access via ``.get``) since
      those columns are free-form JSONB and not schema-enforced.
    - All functions are pure (no I/O, no session access) so they can
      be safely reused by the service layer, background jobs, or
      notification dispatchers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.models.activity import Activity, ActivityModule, ActivityType

__all__ = ["ActivityFormatter"]


class ActivityFormatter:
    """Namespace of static/class methods for formatting activity entries."""

    # ------------------------------------------------------------------
    # Relative time
    # ------------------------------------------------------------------

    @staticmethod
    def relative_time(moment: Optional[datetime]) -> str:
        """Renders a timestamp as a short, human-friendly relative string.

        Args:
            moment: The timestamp to render, timezone-aware or naive
                (assumed UTC if naive). ``None`` yields ``"unknown"``.

        Returns:
            str: A string such as ``"just now"``, ``"5 minutes ago"``,
            ``"3 hours ago"``, ``"2 days ago"``, or ``"on 2026-08-01"``
            for anything older than 30 days.
        """
        if moment is None:
            return "unknown"

        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        delta_seconds = (now - moment).total_seconds()

        if delta_seconds < 0:
            return "just now"
        if delta_seconds < 60:
            return "just now"
        if delta_seconds < 3600:
            minutes = int(delta_seconds // 60)
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        if delta_seconds < 86400:
            hours = int(delta_seconds // 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        if delta_seconds < 2592000:  # 30 days
            days = int(delta_seconds // 86400)
            return f"{days} day{'s' if days != 1 else ''} ago"

        return f"on {moment.date().isoformat()}"

    # ------------------------------------------------------------------
    # Human-readable message
    # ------------------------------------------------------------------

    @classmethod
    def human_readable_message(cls, activity: Activity) -> str:
        """Builds a single-sentence, human-readable summary of an activity.

        Args:
            activity: The activity entry to describe.

        Returns:
            str: A natural-language sentence describing what happened,
            e.g. ``"John Doe updated Booking #42"``.
        """
        actor = cls._actor_label(activity)
        verb = cls._verb_for_action(activity.action)
        target = f"{activity.entity_type} #{activity.entity_id}"
        return f"{actor} {verb} {target}".strip()

    @staticmethod
    def _actor_label(activity: Activity) -> str:
        """Resolves a display label for whoever performed the activity.

        Args:
            activity: The activity entry.

        Returns:
            str: The performing user's full name/email if the relationship
            is loaded, ``"System"`` if unattributed.
        """
        performed_by = getattr(activity, "performed_by", None)
        if performed_by is not None:
            for attr in ("full_name", "name", "email"):
                value = getattr(performed_by, attr, None)
                if value:
                    return str(value)
        return "System"

    @staticmethod
    def _verb_for_action(action: ActivityType) -> str:
        """Maps an :class:`ActivityType` to a natural-language verb phrase.

        Args:
            action: The activity type/action.

        Returns:
            str: A lowercase verb phrase, e.g. ``"created"``, ``"marked as
            completed"``.
        """
        mapping = {
            ActivityType.CREATED: "created",
            ActivityType.UPDATED: "updated",
            ActivityType.DELETED: "deleted",
            ActivityType.RESTORED: "restored",
            ActivityType.ARCHIVED: "archived",
            ActivityType.STATUS_CHANGED: "changed the status of",
            ActivityType.ASSIGNED: "assigned",
            ActivityType.UNASSIGNED: "unassigned",
            ActivityType.APPROVED: "approved",
            ActivityType.REJECTED: "rejected",
            ActivityType.UPLOADED: "uploaded a file to",
            ActivityType.DOWNLOADED: "downloaded a file from",
            ActivityType.VIEWED: "viewed",
            ActivityType.COMMENTED: "commented on",
            ActivityType.SCHEDULED: "scheduled",
            ActivityType.CANCELLED: "cancelled",
            ActivityType.COMPLETED: "marked as completed",
            ActivityType.PAYMENT_RECEIVED: "recorded a payment received for",
            ActivityType.PAYMENT_FAILED: "recorded a failed payment for",
            ActivityType.WORKFLOW_STARTED: "started a workflow for",
            ActivityType.WORKFLOW_COMPLETED: "completed a workflow for",
            ActivityType.NOTIFICATION_SENT: "sent a notification for",
            ActivityType.AI_GENERATED: "generated an AI output for",
            ActivityType.LOGIN: "logged in via",
            ActivityType.LOGOUT: "logged out of",
            ActivityType.EXPORTED: "exported",
            ActivityType.IMPORTED: "imported",
        }
        return mapping.get(action, action.value.replace("_", " "))

    # ------------------------------------------------------------------
    # Timeline card (generic, module-agnostic)
    # ------------------------------------------------------------------

    @classmethod
    def to_timeline_card(cls, activity: Activity) -> dict[str, Any]:
        """Formats an activity as a compact card payload for timeline UIs.

        Args:
            activity: The activity entry to format.

        Returns:
            dict[str, Any]: A UI-ready card with id, headline, message,
            actor, module, action, priority, status, and relative/absolute
            timestamps.
        """
        return {
            "id": str(activity.id),
            "module": activity.module.value,
            "action": activity.action.value,
            "entity_type": activity.entity_type,
            "entity_id": activity.entity_id,
            "title": activity.title,
            "description": activity.description,
            "message": cls.human_readable_message(activity),
            "actor": cls._actor_label(activity),
            "priority": activity.priority.value,
            "status": activity.status.value,
            "created_at": activity.created_at.isoformat()
            if activity.created_at
            else None,
            "relative_time": cls.relative_time(activity.created_at),
        }

    # ------------------------------------------------------------------
    # Module-specific formatters
    # ------------------------------------------------------------------

    @classmethod
    def format_audit(cls, activity: Activity) -> dict[str, Any]:
        """Formats an ``AUDIT``-module activity, surfacing old/new value diffs.

        Args:
            activity: The audit activity entry.

        Returns:
            dict[str, Any]: The base timeline card enriched with an
            ``old_value`` / ``new_value`` change summary.
        """
        card = cls.to_timeline_card(activity)
        card["change"] = {
            "old_value": activity.old_value,
            "new_value": activity.new_value,
        }
        return card

    @classmethod
    def format_notification(cls, activity: Activity) -> dict[str, Any]:
        """Formats a ``NOTIFICATION``-module activity.

        Args:
            activity: The notification activity entry.

        Returns:
            dict[str, Any]: The base timeline card enriched with the
            notification channel/recipient, when present in ``meta_data``.
        """
        meta = activity.meta_data or {}
        card = cls.to_timeline_card(activity)
        card["notification"] = {
            "channel": meta.get("channel"),
            "recipient": meta.get("recipient"),
            "template": meta.get("template"),
        }
        return card

    @classmethod
    def format_workflow(cls, activity: Activity) -> dict[str, Any]:
        """Formats a ``WORKFLOW``-module activity.

        Args:
            activity: The workflow activity entry.

        Returns:
            dict[str, Any]: The base timeline card enriched with workflow
            step/definition context, when present in ``meta_data``.
        """
        meta = activity.meta_data or {}
        card = cls.to_timeline_card(activity)
        card["workflow"] = {
            "workflow_id": meta.get("workflow_id"),
            "step": meta.get("step"),
            "definition": meta.get("definition"),
        }
        return card

    @classmethod
    def format_booking(cls, activity: Activity) -> dict[str, Any]:
        """Formats a ``BOOKING``-module activity.

        Args:
            activity: The booking activity entry.

        Returns:
            dict[str, Any]: The base timeline card enriched with booking
            date/property context, when present in ``meta_data``.
        """
        meta = activity.meta_data or {}
        card = cls.to_timeline_card(activity)
        card["booking"] = {
            "property_id": meta.get("property_id"),
            "scheduled_at": meta.get("scheduled_at"),
            "booking_status": meta.get("booking_status"),
        }
        return card

    @classmethod
    def format_payment(cls, activity: Activity) -> dict[str, Any]:
        """Formats a ``PAYMENT``-module activity.

        Args:
            activity: The payment activity entry.

        Returns:
            dict[str, Any]: The base timeline card enriched with amount/
            currency/method context, when present in ``meta_data``.
        """
        meta = activity.meta_data or {}
        card = cls.to_timeline_card(activity)
        card["payment"] = {
            "amount": meta.get("amount"),
            "currency": meta.get("currency", "INR"),
            "method": meta.get("method"),
            "reference": meta.get("reference"),
        }
        return card

    @classmethod
    def format_property(cls, activity: Activity) -> dict[str, Any]:
        """Formats a ``PROPERTY``-module activity.

        Args:
            activity: The property activity entry.

        Returns:
            dict[str, Any]: The base timeline card enriched with property
            listing context, when present in ``meta_data``.
        """
        meta = activity.meta_data or {}
        card = cls.to_timeline_card(activity)
        card["property"] = {
            "listing_type": meta.get("listing_type"),
            "location": meta.get("location"),
            "price": meta.get("price"),
        }
        return card

    @classmethod
    def format_lead(cls, activity: Activity) -> dict[str, Any]:
        """Formats a ``LEAD``-module activity.

        Args:
            activity: The lead activity entry.

        Returns:
            dict[str, Any]: The base timeline card enriched with lead
            source/stage context, when present in ``meta_data``.
        """
        meta = activity.meta_data or {}
        card = cls.to_timeline_card(activity)
        card["lead"] = {
            "source": meta.get("source"),
            "stage": meta.get("stage"),
            "score": meta.get("score"),
        }
        return card

    @classmethod
    def format_customer(cls, activity: Activity) -> dict[str, Any]:
        """Formats a ``CUSTOMER``-module activity.

        Args:
            activity: The customer activity entry.

        Returns:
            dict[str, Any]: The base timeline card enriched with customer
            segment/contact context, when present in ``meta_data``.
        """
        meta = activity.meta_data or {}
        card = cls.to_timeline_card(activity)
        card["customer"] = {
            "segment": meta.get("segment"),
            "contact_channel": meta.get("contact_channel"),
        }
        return card

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    @classmethod
    def _formatter_for_module(
        cls, module: ActivityModule
    ) -> Callable[[Activity], dict[str, Any]]:
        """Resolves the module-specific formatter callable for an activity.

        Args:
            module: The owning module of the activity.

        Returns:
            Callable[[Activity], dict[str, Any]]: The formatter function to
            apply; falls back to :meth:`to_timeline_card` for modules
            without a dedicated formatter.
        """
        registry: dict[ActivityModule, Callable[[Activity], dict[str, Any]]] = {
            ActivityModule.AUDIT: cls.format_audit,
            ActivityModule.NOTIFICATION: cls.format_notification,
            ActivityModule.WORKFLOW: cls.format_workflow,
            ActivityModule.BOOKING: cls.format_booking,
            ActivityModule.PAYMENT: cls.format_payment,
            ActivityModule.PROPERTY: cls.format_property,
            ActivityModule.LEAD: cls.format_lead,
            ActivityModule.CUSTOMER: cls.format_customer,
        }
        return registry.get(module, cls.to_timeline_card)

    @classmethod
    def format(cls, activity: Activity) -> dict[str, Any]:
        """Formats an activity using the formatter appropriate to its module.

        Args:
            activity: The activity entry to format.

        Returns:
            dict[str, Any]: The formatted, UI-ready payload.
        """
        formatter = cls._formatter_for_module(activity.module)
        return formatter(activity)

    @classmethod
    def format_many(cls, activities: list[Activity]) -> list[dict[str, Any]]:
        """Formats a list of activities using their respective module formatters.

        Args:
            activities: The activity entries to format.

        Returns:
            list[dict[str, Any]]: The formatted payloads, in input order.
        """
        return [cls.format(activity) for activity in activities]