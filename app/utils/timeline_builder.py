"""
backend/app/utils/timeline_builder.py

Reusable Timeline Builder for the Activity Timeline module of the
Enterprise Real Estate AI Copilot CRM.

This module assembles raw ``Activity`` ORM rows (as returned by
``app.repositories.activity_repository.ActivityRepository`` /
``app.services.activity_service.ActivityService``) into UI-ready
timeline payloads: merging feeds from multiple sources, sorting,
grouping by day/module, and computing lightweight aggregate
statistics.

This module is intentionally free of database session and HTTP
concerns -- it operates purely on already-fetched ``Activity``
instances (or plain dicts produced by
:class:`app.utils.activity_formatter.ActivityFormatter`) so it can be
reused by the API layer, background jobs, or notification digesting.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any, Iterable, Literal, Optional

from app.models.activity import Activity
from app.utils.activity_formatter import ActivityFormatter

__all__ = ["TimelineBuilder"]

SortOrder = Literal["asc", "desc"]
GroupKey = Literal["day", "module", "action", "priority", "status"]


class TimelineBuilder:
    """Namespace of static/class methods for assembling activity timelines."""

    # ------------------------------------------------------------------
    # Sorting
    # ------------------------------------------------------------------

    @staticmethod
    def sort_activities(
        activities: Iterable[Activity], *, sort_order: SortOrder = "desc"
    ) -> list[Activity]:
        """Sorts activities chronologically by ``created_at``.

        Args:
            activities: The activity entries to sort.
            sort_order: ``"asc"`` for oldest-first, ``"desc"`` for
                newest-first.

        Returns:
            list[Activity]: A new, sorted list. The input is not mutated.
        """
        return sorted(
            activities,
            key=lambda entry: entry.created_at or datetime.min,
            reverse=(sort_order == "desc"),
        )

    # ------------------------------------------------------------------
    # Merging
    # ------------------------------------------------------------------

    @classmethod
    def merge_activities(
        cls,
        *feeds: Iterable[Activity],
        sort_order: SortOrder = "desc",
        deduplicate: bool = True,
    ) -> list[Activity]:
        """Merges multiple activity feeds into a single, sorted timeline.

        Useful when a view needs to combine, e.g., a booking's own
        timeline with its related payment timeline into one feed.

        Args:
            *feeds: Any number of iterables of activity entries.
            sort_order: ``"asc"`` or ``"desc"`` for the merged result.
            deduplicate: Whether entries with a duplicate ``id`` should be
                collapsed to a single occurrence.

        Returns:
            list[Activity]: The merged, sorted (and optionally
            de-duplicated) list of activities.
        """
        merged: list[Activity] = []
        seen_ids: set = set()

        for feed in feeds:
            for entry in feed:
                if deduplicate:
                    if entry.id in seen_ids:
                        continue
                    seen_ids.add(entry.id)
                merged.append(entry)

        return cls.sort_activities(merged, sort_order=sort_order)

    # ------------------------------------------------------------------
    # Grouping
    # ------------------------------------------------------------------

    @staticmethod
    def group_activities(
        activities: Iterable[Activity], *, group_by: GroupKey = "day"
    ) -> dict[str, list[Activity]]:
        """Groups activities by a calendar day or a categorical attribute.

        Args:
            activities: The activity entries to group.
            group_by: One of ``"day"``, ``"module"``, ``"action"``,
                ``"priority"``, or ``"status"``.

        Returns:
            dict[str, list[Activity]]: Mapping of group key (ISO date
            string for ``"day"``, enum value otherwise) to the activities
            in that group, preserving each group's relative ordering.
        """
        grouped: dict[str, list[Activity]] = defaultdict(list)

        for entry in activities:
            if group_by == "day":
                key = (
                    entry.created_at.date().isoformat()
                    if entry.created_at
                    else "unknown"
                )
            else:
                attr_value = getattr(entry, group_by, None)
                key = attr_value.value if attr_value is not None else "unknown"
            grouped[key].append(entry)

        return dict(grouped)

    @classmethod
    def group_into_cards(
        cls, activities: Iterable[Activity], *, group_by: GroupKey = "day"
    ) -> list[dict[str, Any]]:
        """Groups activities and formats each group as a UI-ready section.

        Args:
            activities: The activity entries to group and format.
            group_by: The grouping key, see :meth:`group_activities`.

        Returns:
            list[dict[str, Any]]: A list of ``{"key": ..., "count": ...,
            "items": [...]}`` sections, ordered by most recent activity
            within each group.
        """
        grouped = cls.group_activities(activities, group_by=group_by)
        sections: list[dict[str, Any]] = []

        for key, entries in grouped.items():
            sorted_entries = cls.sort_activities(entries, sort_order="desc")
            sections.append(
                {
                    "key": key,
                    "count": len(sorted_entries),
                    "items": ActivityFormatter.format_many(sorted_entries),
                }
            )

        sections.sort(
            key=lambda section: section["items"][0]["created_at"] or "",
            reverse=True,
        )
        return sections

    # ------------------------------------------------------------------
    # Timeline builders (entity / user / module)
    # ------------------------------------------------------------------

    @staticmethod
    def build_entity_timeline(
        activities: Iterable[Activity],
        *,
        entity_type: str,
        entity_id: str,
        total_count: int,
    ) -> dict[str, Any]:
        """Builds a full timeline payload scoped to a single entity.

        Args:
            activities: The (already paginated/sorted) activity entries
                for the entity.
            entity_type: The entity/table the timeline belongs to.
            entity_id: The primary key of the entity.
            total_count: The total number of activities for the entity,
                across all pages.

        Returns:
            dict[str, Any]: A payload matching the shape of
            ``app.schemas.activity.TimelineResponse``: entity metadata,
            formatted items, total count, and first/last activity
            timestamps.
        """
        items = list(activities)
        formatted = ActivityFormatter.format_many(items)
        chronological = TimelineBuilder.sort_activities(items, sort_order="asc")

        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "items": formatted,
            "total_count": total_count,
            "first_activity_at": chronological[0].created_at
            if chronological
            else None,
            "last_activity_at": chronological[-1].created_at
            if chronological
            else None,
        }

    @staticmethod
    def build_user_timeline(
        activities: Iterable[Activity],
        *,
        user_id: int,
        total_count: int,
    ) -> dict[str, Any]:
        """Builds a timeline payload for all activity involving a user.

        Args:
            activities: The (already paginated/sorted) activity entries
                where the user is either the performer or the assignee.
            user_id: The user id the timeline is scoped to.
            total_count: The total number of matching activities, across
                all pages.

        Returns:
            dict[str, Any]: A payload with the user id, formatted items,
            total count, and a breakdown of how many entries the user
            performed vs. was assigned.
        """
        items = list(activities)
        performed = sum(1 for entry in items if entry.performed_by_id == user_id)
        assigned = sum(1 for entry in items if entry.assigned_to_id == user_id)

        return {
            "user_id": user_id,
            "items": ActivityFormatter.format_many(items),
            "total_count": total_count,
            "performed_count": performed,
            "assigned_count": assigned,
        }

    @staticmethod
    def build_module_timeline(
        activities: Iterable[Activity],
        *,
        module: str,
        total_count: int,
    ) -> dict[str, Any]:
        """Builds a timeline payload scoped to an entire owning module.

        Args:
            activities: The (already paginated/sorted) activity entries
                for the module.
            module: The owning module the timeline is scoped to.
            total_count: The total number of matching activities, across
                all pages.

        Returns:
            dict[str, Any]: A payload with the module name, formatted
            items, total count, and a per-action breakdown.
        """
        items = list(activities)
        action_counts = Counter(entry.action.value for entry in items)

        return {
            "module": module,
            "items": ActivityFormatter.format_many(items),
            "total_count": total_count,
            "action_breakdown": dict(action_counts),
        }

    # ------------------------------------------------------------------
    # Recent activities
    # ------------------------------------------------------------------

    @staticmethod
    def recent_activities(
        activities: Iterable[Activity], *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Formats the most recent activities as timeline cards.

        Args:
            activities: The candidate activity entries (assumed already
                fetched, e.g. via
                ``ActivityRepository.get_recent_activities``).
            limit: The maximum number of cards to return.

        Returns:
            list[dict[str, Any]]: The most recent entries, newest first,
            formatted as timeline cards.
        """
        sorted_entries = TimelineBuilder.sort_activities(
            activities, sort_order="desc"
        )[:limit]
        return ActivityFormatter.format_many(sorted_entries)

    # ------------------------------------------------------------------
    # Timeline statistics
    # ------------------------------------------------------------------

    @staticmethod
    def timeline_statistics(
        activities: Iterable[Activity],
        *,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Computes aggregate statistics over an in-memory set of activities.

        Intended for lightweight, already-fetched batches (e.g. a single
        entity's timeline). For database-level aggregation over the full
        table, prefer
        ``ActivityRepository.count_by_module`` / ``count_by_action`` /
        ``count_by_status`` / ``count_by_priority``.

        Args:
            activities: The activity entries to summarize.
            date_from: Optional inclusive lower bound label for the window.
            date_to: Optional inclusive upper bound label for the window.

        Returns:
            dict[str, Any]: A payload matching the shape of
            ``app.schemas.activity.StatisticsResponse``.
        """
        items = list(activities)

        return {
            "total_activities": len(items),
            "by_module": dict(Counter(entry.module.value for entry in items)),
            "by_action": dict(Counter(entry.action.value for entry in items)),
            "by_priority": dict(Counter(entry.priority.value for entry in items)),
            "by_status": dict(Counter(entry.status.value for entry in items)),
            "date_from": date_from,
            "date_to": date_to,
        }

    # ------------------------------------------------------------------
    # Day-bucketed activity counts (e.g. for activity heatmaps/sparklines)
    # ------------------------------------------------------------------

    @staticmethod
    def activity_counts_by_day(
        activities: Iterable[Activity],
    ) -> dict[str, int]:
        """Counts activities per calendar day.

        Args:
            activities: The activity entries to bucket.

        Returns:
            dict[str, int]: Mapping of ISO date string to activity count,
            sorted chronologically.
        """
        counts: Counter[date] = Counter()
        for entry in activities:
            if entry.created_at:
                counts[entry.created_at.date()] += 1

        return {
            day.isoformat(): count
            for day, count in sorted(counts.items(), key=lambda item: item[0])
        }