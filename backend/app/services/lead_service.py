"""
backend/app/services/lead_service.py

Service layer implementing lead-management business logic for the Real
Estate AI Copilot CRM.

Responsibilities:
    - Orchestrate lead creation, retrieval, listing, updates, soft
      deletion, agent assignment, status/priority transitions, and
      dashboard aggregation by composing `LeadRepository`.
    - Enforce all business rules (uniqueness constraints, status
      workflow ordering, active/inactive invariants) independently of
      any HTTP or persistence concern.

Design Notes:
    - This service is framework-agnostic: it never imports FastAPI and
      never raises `HTTPException`. All failure conditions are surfaced
      as the domain exceptions defined below; translating them into
      HTTP responses is the API layer's responsibility.
    - This service never constructs its own SQL and never imports
      `AsyncSession`. All persistence access is delegated exclusively
      to the injected `LeadRepository`.
    - The lead status workflow requested for this system (NEW →
      CONTACTED → SITE_VISIT → NEGOTIATION → BOOKED → CLOSED) references
      a `CLOSED` status that does not exist on `LeadStatus` (which
      defines NEW, CONTACTED, QUALIFIED, SITE_VISIT, NEGOTIATION,
      BOOKED, LOST). This service implements the closest faithful
      mapping onto the actual enum: `QUALIFIED` is inserted into the
      natural pipeline position between `CONTACTED` and `SITE_VISIT`,
      and `BOOKED`/`LOST` are treated as the two terminal states (the
      real model's equivalent of "CLOSED"), from which no further
      transitions are permitted.
    - `assign_agent()` validates that `agent_id` is a well-formed
      positive identifier and that the target lead is active. Full
      existence validation of the agent against the `users` table is
      intentionally out of scope here, since this service is
      constructed with only a `LeadRepository` (per the required
      constructor signature) and has no access to a user-data source.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Optional, Sequence

from app.models.lead import Lead, LeadPriority, LeadSource, LeadStatus
from app.repositories.lead_repository import LeadRepository


# --------------------------------------------------------------------------
# Domain Exceptions
# --------------------------------------------------------------------------
class LeadDomainError(Exception):
    """Base class for all lead-domain business rule violations."""


class LeadNotFoundError(LeadDomainError):
    """Raised when a lead cannot be located by its identifier."""

    def __init__(self, lead_id: uuid.UUID) -> None:
        self.lead_id = lead_id
        super().__init__(f"Lead with id '{lead_id}' was not found.")


class DuplicatePhoneError(LeadDomainError):
    """Raised when a lead's phone number collides with an existing lead."""

    def __init__(self, phone: str) -> None:
        self.phone = phone
        super().__init__(f"A lead with phone number '{phone}' already exists.")


class DuplicateEmailError(LeadDomainError):
    """Raised when a lead's email address collides with an existing lead."""

    def __init__(self, email: str) -> None:
        self.email = email
        super().__init__(f"A lead with email address '{email}' already exists.")


class InactiveLeadError(LeadDomainError):
    """Raised when an operation is attempted on an inactive (soft-deleted) lead."""

    def __init__(self, lead_id: uuid.UUID) -> None:
        self.lead_id = lead_id
        super().__init__(f"Lead with id '{lead_id}' is inactive and cannot be modified.")


class TerminalLeadStatusError(LeadDomainError):
    """Raised when attempting to modify a lead that has reached a terminal status."""

    def __init__(self, lead_id: uuid.UUID, status: LeadStatus) -> None:
        self.lead_id = lead_id
        self.status = status
        super().__init__(
            f"Lead with id '{lead_id}' has reached terminal status "
            f"'{status.value}' and cannot be modified further."
        )


class InvalidStatusTransitionError(LeadDomainError):
    """Raised when a requested status transition violates the pipeline workflow."""

    def __init__(self, current_status: LeadStatus, target_status: LeadStatus) -> None:
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(
            f"Cannot transition lead from '{current_status.value}' to "
            f"'{target_status.value}'."
        )


class InvalidAgentAssignmentError(LeadDomainError):
    """Raised when an agent assignment references an invalid agent identifier."""

    def __init__(self, agent_id: int) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent id '{agent_id}' is not a valid assignment target.")


# --------------------------------------------------------------------------
# Lead Service
# --------------------------------------------------------------------------
class LeadService:
    """
    Service encapsulating lead-management business logic: creation,
    retrieval, listing, updates, soft deletion, agent assignment,
    status/priority transitions, dashboard aggregation, and search.

    Consumed by API routers via dependency injection; contains no HTTP
    routing concerns and performs no direct database access.
    """

    # ----------------------------------------------------------------
    # Status Workflow Definition
    # ----------------------------------------------------------------
    _STATUS_WORKFLOW_ORDER: tuple[LeadStatus, ...] = (
        LeadStatus.NEW,
        LeadStatus.CONTACTED,
        LeadStatus.QUALIFIED,
        LeadStatus.SITE_VISIT,
        LeadStatus.NEGOTIATION,
        LeadStatus.BOOKED,
    )

    _TERMINAL_STATUSES: frozenset[LeadStatus] = frozenset(
        {LeadStatus.BOOKED, LeadStatus.LOST}
    )

    def __init__(self, repository: LeadRepository) -> None:
        """
        Args:
            repository: The `LeadRepository` instance used for all
                        persistence operations.
        """
        self._repository = repository

    # ----------------------------------------------------------------
    # Create
    # ----------------------------------------------------------------
    async def create_lead(self, data: dict[str, Any]) -> Lead:
        """
        Create a new lead after validating phone/email uniqueness.

        Args:
            data: Mapping of `Lead` field names to values, typically
                  derived from a validated `LeadCreate` schema by the
                  API layer.

        Returns:
            The newly created and persisted `Lead` instance.

        Raises:
            DuplicatePhoneError: If a lead with the given phone already
                exists.
            DuplicateEmailError: If a lead with the given email already
                exists.
        """
        phone = data.get("phone")
        email = data.get("email")

        if phone:
            await self.validate_phone_duplicate(phone)
        if email:
            await self.validate_email_duplicate(email)

        lead = Lead(**data)
        return await self._repository.create_lead(lead)

    # ----------------------------------------------------------------
    # Read - Single Record
    # ----------------------------------------------------------------
    async def get_lead(self, lead_id: uuid.UUID) -> Lead:
        """
        Retrieve a lead by its identifier.

        Args:
            lead_id: The UUID of the lead to retrieve.

        Returns:
            The matching `Lead` instance.

        Raises:
            LeadNotFoundError: If no lead with the given ID exists.
        """
        lead = await self._repository.get_by_id(lead_id)
        if lead is None:
            raise LeadNotFoundError(lead_id)
        return lead

    # ----------------------------------------------------------------
    # Read - Listing / Search
    # ----------------------------------------------------------------
    async def list_leads(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[LeadStatus] = None,
        priority: Optional[LeadPriority] = None,
        lead_source: Optional[LeadSource] = None,
        assigned_agent_id: Optional[int] = None,
        property_type: Optional[str] = None,
        preferred_location: Optional[str] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        search: Optional[str] = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        """
        Retrieve a paginated, filtered, and sorted list of leads.

        Args:
            page: 1-indexed page number to retrieve.
            page_size: Number of records to return per page.
            status: Optional pipeline status filter.
            priority: Optional priority filter.
            lead_source: Optional acquisition channel filter.
            assigned_agent_id: Optional assigned agent filter.
            property_type: Optional property type filter.
            preferred_location: Optional preferred location filter.
            phone: Optional exact-match contact phone number filter.
            email: Optional exact-match contact email address filter.
            search: Optional free-text search term.
            sort_by: Column name to sort by.
            sort_order: "asc" or "desc".

        Returns:
            A dict containing `items`, `total`, `page`, `page_size`,
            and `total_pages`, suitable for constructing a
            `LeadListResponse` in the API layer.
        """
        leads, total = await self._repository.list_leads(
            page=page,
            page_size=page_size,
            status=status,
            priority=priority,
            lead_source=lead_source,
            assigned_agent_id=assigned_agent_id,
            property_type=property_type,
            preferred_location=preferred_location,
            phone=phone,
            email=email,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return self._build_paginated_result(leads, total, page, page_size)

    async def search(
        self,
        term: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """
        Perform a free-text search for leads across name, phone, email,
        and remarks fields.

        Args:
            term: The search term to match.
            page: 1-indexed page number to retrieve.
            page_size: Number of records to return per page.

        Returns:
            A dict containing `items`, `total`, `page`, `page_size`,
            and `total_pages`.
        """
        leads, total = await self._repository.search(term, page=page, page_size=page_size)
        return self._build_paginated_result(leads, total, page, page_size)

    async def upcoming_followups(self, *, reference_date: Optional[date] = None) -> Sequence[Lead]:
        """
        Retrieve all active leads whose next follow-up is due today (or
        earlier).

        Args:
            reference_date: The date to compare against. Defaults to
                today if not supplied.

        Returns:
            A sequence of matching `Lead` instances.
        """
        return await self._repository.upcoming_followups(reference_date=reference_date)

    # ----------------------------------------------------------------
    # Update
    # ----------------------------------------------------------------
    async def update_lead(self, lead_id: uuid.UUID, data: dict[str, Any]) -> Lead:
        """
        Update an existing lead's fields, enforcing phone/email
        uniqueness for any changed values.

        Args:
            lead_id: The UUID of the lead to update.
            data: Mapping of field names to new values. Only keys
                  present in this mapping are applied (PATCH semantics).

        Returns:
            The updated `Lead` instance.

        Raises:
            LeadNotFoundError: If no lead with the given ID exists.
            InactiveLeadError: If the lead is inactive.
            DuplicatePhoneError: If the new phone collides with another
                lead.
            DuplicateEmailError: If the new email collides with another
                lead.
        """
        lead = await self.get_lead(lead_id)
        self._ensure_active(lead)

        new_phone = data.get("phone")
        if new_phone and new_phone != lead.phone:
            await self.validate_phone_duplicate(new_phone, exclude_lead_id=lead_id)

        new_email = data.get("email")
        if new_email and new_email != lead.email:
            await self.validate_email_duplicate(new_email, exclude_lead_id=lead_id)

        # NOTE: `data` already reflects PATCH semantics via the API
        # layer's `LeadUpdate.model_dump(exclude_unset=True)` -- only
        # keys the client actually supplied are present here at all.
        # Previously this method additionally stripped out any key
        # whose *value* was None, which silently prevented clients
        # from ever clearing a nullable field (e.g. `remarks`,
        # `next_follow_up`, `assigned_agent_id`) via update, contrary
        # to this method's own documented "only keys present... are
        # applied" contract. An explicit None for a non-nullable
        # column (e.g. `full_name`, `phone`) is still safely rejected
        # by the database's NOT NULL constraint, surfaced as a clean
        # 409 via the existing IntegrityError handler.
        return await self._repository.update_lead(lead, data)

    # ----------------------------------------------------------------
    # Delete
    # ----------------------------------------------------------------
    async def delete_lead(self, lead_id: uuid.UUID) -> Lead:
        """
        Soft-delete a lead by marking it inactive.

        Args:
            lead_id: The UUID of the lead to soft-delete.

        Returns:
            The soft-deleted `Lead` instance.

        Raises:
            LeadNotFoundError: If no lead with the given ID exists.
        """
        await self.get_lead(lead_id)
        deleted_lead = await self._repository.soft_delete(lead_id)
        if deleted_lead is None:
            raise LeadNotFoundError(lead_id)
        return deleted_lead

    # ----------------------------------------------------------------
    # Agent Assignment
    # ----------------------------------------------------------------
    async def assign_agent(self, lead_id: uuid.UUID, agent_id: int) -> Lead:
        """
        Assign a sales agent to a lead.

        Args:
            lead_id: The UUID of the lead to update.
            agent_id: The internal User ID of the agent to assign.

        Returns:
            The updated `Lead` instance.

        Raises:
            LeadNotFoundError: If no lead with the given ID exists.
            InactiveLeadError: If the lead is inactive.
            InvalidAgentAssignmentError: If `agent_id` is not a valid
                positive identifier.
        """
        if agent_id <= 0:
            raise InvalidAgentAssignmentError(agent_id)

        lead = await self.get_lead(lead_id)
        self._ensure_active(lead)

        updated_lead = await self._repository.assign_agent(lead_id, agent_id)
        if updated_lead is None:
            raise LeadNotFoundError(lead_id)
        return updated_lead

    # ----------------------------------------------------------------
    # Status Transition
    # ----------------------------------------------------------------
    async def change_status(self, lead_id: uuid.UUID, target_status: LeadStatus) -> Lead:
        """
        Transition a lead to a new pipeline status, enforcing the
        forward-only workflow ordering and terminal-state immutability.

        Allowed forward progression:
            NEW -> CONTACTED -> QUALIFIED -> SITE_VISIT -> NEGOTIATION
            -> BOOKED

        `LOST` is reachable from any non-terminal status (a lead may be
        marked lost at any stage of the pipeline). `BOOKED` and `LOST`
        are terminal: once reached, no further status changes are
        permitted.

        Args:
            lead_id: The UUID of the lead to update.
            target_status: The desired new `LeadStatus`.

        Returns:
            The updated `Lead` instance.

        Raises:
            LeadNotFoundError: If no lead with the given ID exists.
            InactiveLeadError: If the lead is inactive.
            TerminalLeadStatusError: If the lead has already reached a
                terminal status.
            InvalidStatusTransitionError: If the transition violates
                the forward-only workflow ordering.
        """
        lead = await self.get_lead(lead_id)
        self._ensure_active(lead)

        current_status = lead.status

        if current_status in self._TERMINAL_STATUSES:
            raise TerminalLeadStatusError(lead_id, current_status)

        if target_status != LeadStatus.LOST:
            if (
                current_status not in self._STATUS_WORKFLOW_ORDER
                or target_status not in self._STATUS_WORKFLOW_ORDER
            ):
                raise InvalidStatusTransitionError(current_status, target_status)

            current_index = self._STATUS_WORKFLOW_ORDER.index(current_status)
            target_index = self._STATUS_WORKFLOW_ORDER.index(target_status)

            if target_index <= current_index:
                raise InvalidStatusTransitionError(current_status, target_status)

            if target_index != current_index + 1:
                raise InvalidStatusTransitionError(current_status, target_status)

        updated_lead = await self._repository.change_status(lead_id, target_status)
        if updated_lead is None:
            raise LeadNotFoundError(lead_id)
        return updated_lead

    # ----------------------------------------------------------------
    # Priority Transition
    # ----------------------------------------------------------------
    async def change_priority(self, lead_id: uuid.UUID, priority: LeadPriority) -> Lead:
        """
        Update the priority level of a lead.

        Args:
            lead_id: The UUID of the lead to update.
            priority: The new `LeadPriority` value. Must be a valid
                      `LeadPriority` enum member.

        Returns:
            The updated `Lead` instance.

        Raises:
            LeadNotFoundError: If no lead with the given ID exists.
            InactiveLeadError: If the lead is inactive.
            ValueError: If `priority` is not a valid `LeadPriority`.
        """
        if not isinstance(priority, LeadPriority):
            raise ValueError(f"'{priority}' is not a valid LeadPriority value.")

        lead = await self.get_lead(lead_id)
        self._ensure_active(lead)

        updated_lead = await self._repository.change_priority(lead_id, priority)
        if updated_lead is None:
            raise LeadNotFoundError(lead_id)
        return updated_lead

    # ----------------------------------------------------------------
    # Dashboard Aggregation
    # ----------------------------------------------------------------
    async def dashboard_summary(self) -> dict[str, Any]:
        """
        Compute a summary of key lead-pipeline metrics for dashboard
        display.

        Returns:
            A dict containing:
                - total_leads: Total count of active leads.
                - new_leads: Count of leads with status NEW.
                - contacted_leads: Count of leads with status CONTACTED.
                - booked_leads: Count of leads with status BOOKED.
                - closed_leads: Count of leads in a terminal status
                  (BOOKED or LOST) — the real-model equivalent of
                  "closed".
                - status_counts: Mapping of status value to count.
                - source_counts: Mapping of lead source value to count.
        """
        total_leads = await self._repository.count()
        status_counts = await self._repository.count_by_status()
        source_counts = await self._repository.count_by_source()

        closed_leads = status_counts.get(LeadStatus.BOOKED.value, 0) + status_counts.get(
            LeadStatus.LOST.value, 0
        )

        return {
            "total_leads": total_leads,
            "new_leads": status_counts.get(LeadStatus.NEW.value, 0),
            "contacted_leads": status_counts.get(LeadStatus.CONTACTED.value, 0),
            "booked_leads": status_counts.get(LeadStatus.BOOKED.value, 0),
            "closed_leads": closed_leads,
            "status_counts": status_counts,
            "source_counts": source_counts,
        }

    # ----------------------------------------------------------------
    # Duplicate Validation
    # ----------------------------------------------------------------
    async def validate_phone_duplicate(
        self,
        phone: str,
        *,
        exclude_lead_id: Optional[uuid.UUID] = None,
    ) -> None:
        """
        Ensure no other lead is already registered with the given phone
        number.

        Args:
            phone: The phone number to check.
            exclude_lead_id: Optional lead ID to exclude from the
                collision check (used when updating a lead's own,
                unchanged phone number).

        Raises:
            DuplicatePhoneError: If another lead already has this phone
                number.
        """
        existing = await self._repository.get_by_phone(phone)
        if existing is not None and existing.id != exclude_lead_id:
            raise DuplicatePhoneError(phone)

    async def validate_email_duplicate(
        self,
        email: str,
        *,
        exclude_lead_id: Optional[uuid.UUID] = None,
    ) -> None:
        """
        Ensure no other lead is already registered with the given email
        address.

        Args:
            email: The email address to check.
            exclude_lead_id: Optional lead ID to exclude from the
                collision check (used when updating a lead's own,
                unchanged email address).

        Raises:
            DuplicateEmailError: If another lead already has this email
                address.
        """
        existing = await self._repository.get_by_email(email)
        if existing is not None and existing.id != exclude_lead_id:
            raise DuplicateEmailError(email)

    # ----------------------------------------------------------------
    # Private Helpers
    # ----------------------------------------------------------------
    def _ensure_active(self, lead: Lead) -> None:
        """
        Guard against mutating an inactive (soft-deleted) lead.

        Args:
            lead: The `Lead` instance to check.

        Raises:
            InactiveLeadError: If the lead's `is_active` flag is False.
        """
        if not lead.is_active:
            raise InactiveLeadError(lead.id)

    def _build_paginated_result(
        self,
        items: Sequence[Lead],
        total: int,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        """
        Assemble a standardized pagination result dict.

        Args:
            items: The page of `Lead` records.
            total: Total number of matching records across all pages.
            page: The current 1-indexed page number.
            page_size: The number of records requested per page.

        Returns:
            A dict containing `items`, `total`, `page`, `page_size`,
            and `total_pages`.
        """
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }


__all__ = [
    "LeadService",
    "LeadDomainError",
    "LeadNotFoundError",
    "DuplicatePhoneError",
    "DuplicateEmailError",
    "InactiveLeadError",
    "TerminalLeadStatusError",
    "InvalidStatusTransitionError",
    "InvalidAgentAssignmentError",
]