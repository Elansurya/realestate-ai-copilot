"""
backend/app/services/customer_service.py

Service layer for the Customer domain.

Responsibilities (and only these):
    - Business Logic / Validation
    - Repository Orchestration
    - Transaction-failure Recovery
    - Exception Mapping (repository/DB failures -> domain exceptions)
    - Audit Logging

Explicitly NOT here: SQL, ORM query construction, FastAPI routing, HTTP
request/response handling. Those stay in `CustomerRepository` and the
API layer respectively.

--------------------------------------------------------------------
TRANSACTION OWNERSHIP (read this before touching commit/rollback)
--------------------------------------------------------------------
    * `CustomerRepository` OWNS every commit. Each of its write
      methods (`create`, `update`, `assign_customer`,
      `unassign_customer`, `update_status`, `update_followup`,
      `soft_delete`, `restore`, `delete`) calls
      `await self._db.commit()` internally, as a single atomic unit.
    * `CustomerService` NEVER calls `session.commit()`. Doing so would
      be a no-op-or-worse double commit against a transaction the
      repository already closed.
    * `CustomerService` DOES call `session.rollback()`, but only
      inside `_map_write_failure()`, and only when a repository write
      call raised (e.g. a `commit()` that failed on a DB constraint,
      such as a concurrent unique-email violation). This exists
      because the approved repository does not roll back on its own
      failure — without this, the shared `AsyncSession` would be left
      in a failed-transaction state for the remainder of the request.
    * In short: repository commits on success; service rolls back on
      failure. The service is never a second writer to the same
      transaction the repository just closed.
--------------------------------------------------------------------

--------------------------------------------------------------------
ALIGNMENT WITH THE APPROVED CODEBASE
--------------------------------------------------------------------
Generated against the actual approved files supplied for review:
    - `app.repositories.customer_repository.CustomerRepository`
    - `app.models.user.User` (`id: int`, autoincrement PK)
    - `app.models.lead.Lead` (`id: uuid.UUID`, `gen_random_uuid()` PK)
    - `app.schemas.customer` (`CustomerCreate`, `CustomerUpdate`,
      `CustomerSearchFilters`, `CustomerExportRequest`,
      `CustomerStatisticsResponse`)

Every repository call below uses the real method name and signature
from the approved `CustomerRepository` — no invented methods
(`exists_by_email`, `get_all_for_export`, `assign`, ...) remain. IDs
match the approved models exactly: `Customer.id`/`Lead.id` are
`uuid.UUID`; `User.id` and every `*_by_id`/`assigned_to_id` foreign key
is `int`. `phone` is treated as non-unique, per the approved
repository's own documented behavior.

--------------------------------------------------------------------
REMAINING, UNAVOIDABLE ASSUMPTIONS
--------------------------------------------------------------------
Still not part of any review round, so kept minimal and isolated to
the constructor + the two `_assert_*_exists` calls in
`_CustomerValidators`:

    - `app.repositories.lead_repository.LeadRepository`
          async def get_by_id(self, lead_id: uuid.UUID) -> Lead | None
    - `app.repositories.user_repository.UserRepository`
          async def get_by_id(self, user_id: int) -> User | None
    - `app.exceptions` is assumed to expose:
          CustomerNotFoundError, DuplicateCustomerError,
          InvalidCustomerStateError, LeadNotFoundError,
          UserNotFoundError, CustomerServiceError

If these differ, only the import block and `_CustomerValidators` need
to change.

--------------------------------------------------------------------
MODULE LAYOUT
--------------------------------------------------------------------
Per review feedback that the service was getting large, it is now
composed from three small, independently testable collaborators
instead of one flat class, while still living in this single required
output file:

    - `_AuditLogger`        structured logging wrapper (redaction +
                             consistent event envelope)
    - `_CustomerValidators` existence/uniqueness business-rule checks
    - `_CustomerExporter`   export column selection/projection
    - `CustomerService`     orchestration entry point; composes the
                             three above rather than inlining them
--------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    CustomerNotFoundError,
    CustomerServiceError,
    DuplicateCustomerError,
    InvalidCustomerStateError,
    LeadNotFoundError,
    UserNotFoundError,
)
from app.models.customer import Customer, CustomerStatus
from app.repositories.customer_repository import CustomerRepository
from app.repositories.lead_repository import LeadRepository
from app.repositories.user_repository import UserRepository
from app.schemas.customer import (
    CustomerCreate,
    CustomerExportRequest,
    CustomerSearchFilters,
    CustomerStatisticsResponse,
    CustomerUpdate,
)

#: Fields that must never appear in a log record, per compliance
#: requirements. Does NOT apply to export payloads themselves — export
#: inclusion of KYC data is governed by `CustomerExportRequest`.
_SENSITIVE_LOG_FIELDS: frozenset[str] = frozenset(
    {"pan_number", "aadhaar_number", "passport_number"}
)

#: Default export column set when `CustomerExportRequest.fields` is not
#: supplied.
_DEFAULT_EXPORT_COLUMNS: tuple[str, ...] = (
    "id",
    "first_name",
    "last_name",
    "email",
    "phone",
    "customer_type",
    "customer_source",
    "status",
    "city",
    "preferred_city",
    "budget_min",
    "budget_max",
    "assigned_to_id",
    "next_followup_date",
    "created_at",
)

_KYC_EXPORT_COLUMNS: tuple[str, ...] = ("pan_number", "aadhaar_number", "passport_number")

#: Keys on `CustomerSearchFilters` that `CustomerRepository.export()`
#: does not accept (it has no pagination/sorting concept of its own).
_EXPORT_UNSUPPORTED_FILTER_KEYS: frozenset[str] = frozenset(
    {"page", "page_size", "sort_by", "sort_order"}
)


def _redact(data: dict[str, Any]) -> dict[str, Any]:
    """Returns a copy of ``data`` with sensitive KYC fields redacted.

    Module-level (not a method) so every collaborator below shares one
    redaction rule instead of each maintaining its own copy.

    Args:
        data: Arbitrary field/value mapping that may contain PAN,
            Aadhaar, or passport values.

    Returns:
        A shallow copy of ``data`` with any key in
        ``_SENSITIVE_LOG_FIELDS`` replaced by ``"***REDACTED***"``.
    """
    return {
        key: ("***REDACTED***" if key.lower() in _SENSITIVE_LOG_FIELDS else value)
        for key, value in data.items()
    }


# ==========================================================================
# Structured audit logging
# ==========================================================================
class _AuditLogger:
    """Thin structured-logging wrapper used by every audit call site.

    Centralizes three concerns that would otherwise be repeated at
    every `logger.info(...)` call: sensitive-field redaction, a
    consistent event envelope (service name, UTC timestamp, event
    name), and a single place to swap the underlying handler (e.g. for
    a JSON log shipper) without touching business logic.
    """

    def __init__(self, logger: logging.Logger, *, service_name: str) -> None:
        """
        Args:
            logger: The underlying standard-library logger to emit to.
            service_name: Static label attached to every event,
                identifying the emitting service in aggregated logs.
        """
        self._logger = logger
        self._service_name = service_name

    def emit(self, event: str, **fields: Any) -> None:
        """Emits one structured, redacted audit event.

        Args:
            event: Short, stable event name (e.g. ``"customer_created"``).
            **fields: Contextual fields to attach to the event. Any key
                matching ``_SENSITIVE_LOG_FIELDS`` is redacted before
                emission.
        """
        envelope = {
            "service": self._service_name,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **_redact(fields),
        }
        self._logger.info(event, extra={"audit": envelope})


# ==========================================================================
# Business-rule validators
# ==========================================================================
class _CustomerValidators:
    """Existence and uniqueness checks shared across service methods.

    Extracted from `CustomerService` so validation rules can be unit
    tested (and reused, e.g. by a future bulk-import service) without
    instantiating the full orchestration class.
    """

    def __init__(
        self,
        customer_repository: CustomerRepository,
        lead_repository: LeadRepository,
        user_repository: UserRepository,
    ) -> None:
        self._customer_repository = customer_repository
        self._lead_repository = lead_repository
        self._user_repository = user_repository

    async def assert_email_available(
        self, email: Optional[str], *, exclude_id: Optional[uuid.UUID] = None
    ) -> None:
        """Validates that an email address is not already in use.

        `email` has a real DB-level unique constraint on the approved
        model; this is a pre-check for a clean error message, not the
        sole guard — `CustomerService._map_write_failure` still
        catches a concurrent `IntegrityError` as a safety net.

        Args:
            email: Candidate email address. No-op if ``None``.
            exclude_id: Customer id to exclude from the match (used
                when updating a customer whose own email is unchanged).

        Raises:
            DuplicateCustomerError: If another customer already uses
                this email.
        """
        if not email:
            return
        existing = await self._customer_repository.get_by_email(email)
        if existing is not None and existing.id != exclude_id:
            raise DuplicateCustomerError(f"A customer with email '{email}' already exists.")

    async def assert_lead_exists(self, lead_id: Optional[uuid.UUID]) -> None:
        """Validates that a referenced lead exists.

        Args:
            lead_id: Candidate lead id. No-op if ``None``.

        Raises:
            LeadNotFoundError: If the lead does not exist.
        """
        if lead_id is None:
            return
        lead = await self._lead_repository.get_by_id(lead_id)
        if lead is None:
            raise LeadNotFoundError(f"Lead '{lead_id}' was not found.")

    async def assert_user_exists(self, user_id: Optional[int]) -> None:
        """Validates that a referenced (assignee) user exists.

        Args:
            user_id: Candidate user id. No-op if ``None``.

        Raises:
            UserNotFoundError: If the user does not exist.
        """
        if user_id is None:
            return
        user = await self._user_repository.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError(f"User '{user_id}' was not found.")


# ==========================================================================
# Export projection
# ==========================================================================
class _CustomerExporter:
    """Builds export-ready row dictionaries from `Customer` ORM instances.

    Isolated from `CustomerService` because column selection/whitelist
    logic is pure data transformation with no repository or session
    dependency — it is trivially unit-testable on its own.
    """

    @staticmethod
    def resolve_columns(request: CustomerExportRequest) -> list[str]:
        """Determines the final, validated set of export columns.

        Args:
            request: The export request, carrying an optional explicit
                ``fields`` list and an ``include_kyc_fields`` flag.

        Returns:
            An ordered list of column names, each guaranteed to be a
            real mapped column on ``Customer`` — an unknown name in
            ``request.fields`` is silently dropped rather than passed
            through to ``getattr``.
        """
        valid_columns = set(Customer.__table__.columns.keys())
        if request.fields:
            return [column for column in request.fields if column in valid_columns]
        columns = list(_DEFAULT_EXPORT_COLUMNS)
        if request.include_kyc_fields:
            columns.extend(_KYC_EXPORT_COLUMNS)
        return columns

    @staticmethod
    def to_rows(customers: Sequence[Customer], columns: list[str]) -> list[dict[str, Any]]:
        """Projects ORM instances down to plain dicts of the given columns.

        `getattr` is safe here even for a hybrid/computed property,
        since `columns` is always pre-validated by `resolve_columns`
        (or, for defaults, hardcoded to real mapped columns) before
        this is called.

        Args:
            customers: The customer records to project.
            columns: Column names to include in each row, in order.

        Returns:
            One dict per customer, containing only the requested
            columns.
        """
        return [{column: getattr(customer, column, None) for column in columns} for customer in customers]


# ==========================================================================
# Orchestration entry point
# ==========================================================================
class CustomerService:
    """Business logic and orchestration layer for the Customer domain.

    Composes `_AuditLogger`, `_CustomerValidators`, and
    `_CustomerExporter` rather than inlining their responsibilities,
    keeping this class focused on sequencing repository calls and
    mapping their outcomes to domain exceptions.

    Attributes:
        _customer_repository: Data-access repository for Customer
            records. Owns its own commit boundary per write call.
        _session: The async SQLAlchemy session shared with the
            repositories. Used exclusively for post-failure
            `rollback()` — see the module-level "TRANSACTION
            OWNERSHIP" note above. Never used to `commit()`.
        _validators: Existence/uniqueness business-rule checks.
        _audit: Structured audit-logging wrapper.
    """

    def __init__(
        self,
        customer_repository: CustomerRepository,
        lead_repository: LeadRepository,
        user_repository: UserRepository,
        session: AsyncSession,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """Initializes the service with its injected dependencies.

        Args:
            customer_repository: Repository providing Customer
                persistence operations.
            lead_repository: Repository providing Lead lookups.
            user_repository: Repository providing User lookups.
            session: The async SQLAlchemy session shared with the
                repositories above.
            logger: Optional standard-library logger override, mainly
                for tests. Defaults to the module logger.
        """
        self._customer_repository = customer_repository
        self._session = session
        self._validators = _CustomerValidators(customer_repository, lead_repository, user_repository)
        self._exporter = _CustomerExporter()
        self._audit = _AuditLogger(
            logger or logging.getLogger("app.services.customer_service"),
            service_name="customer_service",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _map_write_failure(self, exc: Exception, context: str) -> None:
        """Rolls back the session and maps an unexpected write failure.

        Called only around a repository *write* call (create/update/
        assign/.../delete) — see the "TRANSACTION OWNERSHIP" note at
        the top of this module for why the rollback lives here rather
        than in the repository.

        Args:
            exc: The exception raised by the repository call.
            context: Short label describing the operation, used for
                logging only.

        Raises:
            DuplicateCustomerError: If the failure was a unique-
                constraint violation (race condition on `email`, since
                the pre-check cannot fully eliminate concurrent
                inserts).
            CustomerServiceError: For any other unexpected failure.
        """
        await self._session.rollback()
        if isinstance(exc, IntegrityError):
            self._audit.emit("customer_integrity_violation", context=context)
            raise DuplicateCustomerError("A customer with this email already exists.") from None
        self._audit.emit("customer_operation_failed", context=context, error=str(exc))
        raise CustomerServiceError(f"Failed to complete operation: {context}.") from None

    async def _get_existing_customer(self, customer_id: uuid.UUID) -> Customer:
        """Fetches a customer by id or raises if it does not exist.

        Args:
            customer_id: Unique identifier of the customer.

        Returns:
            The matching ``Customer`` record.

        Raises:
            CustomerNotFoundError: If no customer exists with that id.
        """
        customer = await self._customer_repository.get_by_id(customer_id)
        if customer is None:
            raise CustomerNotFoundError(f"Customer '{customer_id}' was not found.")
        return customer

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create_customer(self, payload: CustomerCreate, created_by_id: int) -> Customer:
        """Creates a new customer after enforcing business rules.

        Validates email uniqueness and, if provided, the existence of
        the referenced lead and assigned user. Only fields defined on
        ``CustomerCreate`` are ever used to build the ORM instance
        (mass-assignment is impossible by construction), and
        server-controlled audit fields are stamped here rather than
        trusted from the payload.

        Args:
            payload: Validated customer creation data.
            created_by_id: Internal id of the acting user.

        Returns:
            The newly created ``Customer``.

        Raises:
            DuplicateCustomerError: If the email is already in use.
            LeadNotFoundError: If ``payload.lead_id`` is set but does
                not reference an existing lead.
            UserNotFoundError: If ``payload.assigned_to_id`` is set but
                does not reference an existing user.
            CustomerServiceError: On unexpected persistence failure.
        """
        data = payload.model_dump()

        await self._validators.assert_email_available(data.get("email"))
        await self._validators.assert_lead_exists(data.get("lead_id"))
        await self._validators.assert_user_exists(data.get("assigned_to_id"))

        # Server-controlled audit fields — never trusted from the
        # payload even though CustomerCreate doesn't expose them.
        data["created_by_id"] = created_by_id
        data["updated_by_id"] = created_by_id
        data["is_active"] = True

        customer = Customer(**data)

        try:
            created = await self._customer_repository.create(customer)
        except (IntegrityError, SQLAlchemyError) as exc:
            await self._map_write_failure(exc, "create_customer")
            raise  # pragma: no cover - _map_write_failure always raises

        self._audit.emit(
            "customer_created",
            customer_id=str(created.id),
            created_by_id=created_by_id,
            **{k: v for k, v in data.items() if k not in {"created_by_id", "updated_by_id"}},
        )
        return created

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_customer(
        self, customer_id: uuid.UUID, *, is_active: Optional[bool] = None
    ) -> Customer:
        """Retrieves a customer by id.

        Args:
            customer_id: Unique identifier of the customer.
            is_active: Optional active-flag filter forwarded to the
                repository. Defaults to ``None`` (return regardless of
                active state), matching admin/detail-view use cases.

        Returns:
            The matching ``Customer``.

        Raises:
            CustomerNotFoundError: If no customer exists with that id.
        """
        customer = await self._customer_repository.get_by_id(customer_id, is_active=is_active)
        if customer is None:
            raise CustomerNotFoundError(f"Customer '{customer_id}' was not found.")
        return customer

    async def get_customer_by_email(self, email: str) -> Customer:
        """Retrieves a customer by email address.

        Args:
            email: Email address to look up.

        Returns:
            The matching ``Customer``.

        Raises:
            CustomerNotFoundError: If no customer exists with that email.
        """
        customer = await self._customer_repository.get_by_email(email)
        if customer is None:
            raise CustomerNotFoundError(f"Customer with email '{email}' was not found.")
        return customer

    async def get_customer_by_phone(self, phone: str) -> Customer:
        """Retrieves the most recently created customer with this phone.

        `phone` has no uniqueness constraint on the approved model, so
        (matching `CustomerRepository.get_by_phone`'s own documented
        behavior) this returns the most recently created match rather
        than assuming exactly one exists.

        Args:
            phone: Phone number to look up.

        Returns:
            The most recently created matching ``Customer``.

        Raises:
            CustomerNotFoundError: If no customer exists with that phone.
        """
        customer = await self._customer_repository.get_by_phone(phone)
        if customer is None:
            raise CustomerNotFoundError(f"Customer with phone '{phone}' was not found.")
        return customer

    async def list_customers(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        is_active: Optional[bool] = True,
    ) -> tuple[Sequence[Customer], int]:
        """Lists customers with pagination, unfiltered by search terms.

        Args:
            page: 1-indexed page number.
            page_size: Number of records per page.
            sort_by: API-facing sort key (see
                ``CustomerRepository.SORTABLE_FIELDS``).
            sort_order: ``"asc"`` or ``"desc"``.
            is_active: Optional active-flag filter; defaults to
                ``True``.

        Returns:
            A tuple of ``(customers for the page, total matching
            count)``.
        """
        return await self._customer_repository.list(
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            is_active=is_active,
        )

    async def search_customers(
        self, filters: CustomerSearchFilters
    ) -> tuple[Sequence[Customer], int]:
        """Searches customers using the full enterprise filter set.

        ``CustomerSearchFilters`` fields are forwarded directly to
        ``CustomerRepository.search()`` — the two were designed to
        mirror each other 1:1, so no field-by-field translation layer
        is introduced here.

        Args:
            filters: Validated search/filter/pagination/sort
                parameters.

        Returns:
            A tuple of ``(customers for the page, total matching
            count)``.
        """
        return await self._customer_repository.search(**filters.model_dump())

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update_customer(
        self, customer_id: uuid.UUID, payload: CustomerUpdate, updated_by_id: int
    ) -> Customer:
        """Updates an existing customer's mutable fields.

        Only fields explicitly set on ``payload`` are applied. The
        repository's own ``_MUTABLE_COLUMNS`` whitelist is the final
        backstop against mass assignment; this service additionally
        never forwards ``id``/``created_at``/``updated_at``/
        ``created_by_id`` as defense in depth (the repository would
        already ignore them, but the update log stays cleaner this
        way, since `updated_at` is server/refresh-managed and would
        otherwise show up as a spurious "changed field").

        Args:
            customer_id: Id of the customer to update.
            payload: Validated partial update data.
            updated_by_id: Internal id of the acting user.

        Returns:
            The updated ``Customer``.

        Raises:
            CustomerNotFoundError: If the customer does not exist.
            DuplicateCustomerError: If a new email collides with
                another customer.
            LeadNotFoundError: If a new ``lead_id`` does not exist.
            UserNotFoundError: If a new ``assigned_to_id`` does not
                exist.
            CustomerServiceError: On unexpected persistence failure.
        """
        existing = await self._get_existing_customer(customer_id)
        data = payload.model_dump(exclude_unset=True)

        if "email" in data:
            await self._validators.assert_email_available(data["email"], exclude_id=customer_id)
        if "lead_id" in data:
            await self._validators.assert_lead_exists(data["lead_id"])
        if "assigned_to_id" in data:
            await self._validators.assert_user_exists(data["assigned_to_id"])

        data.pop("id", None)
        data.pop("created_at", None)
        data.pop("updated_at", None)
        data.pop("created_by_id", None)
        data["updated_by_id"] = updated_by_id

        try:
            updated = await self._customer_repository.update(existing, data)
        except (IntegrityError, SQLAlchemyError) as exc:
            await self._map_write_failure(exc, "update_customer")
            raise  # pragma: no cover

        self._audit.emit(
            "customer_updated",
            customer_id=str(customer_id),
            updated_by_id=updated_by_id,
            changed_fields=list(_redact(data).keys()),
        )
        return updated

    # ------------------------------------------------------------------
    # Delete / Soft-delete / Restore
    # ------------------------------------------------------------------

    async def delete_customer(self, customer_id: uuid.UUID, deleted_by_id: int) -> None:
        """Permanently deletes a customer record.

        Provided for completeness (e.g. GDPR erasure); routine
        deactivation should use ``soft_delete_customer`` instead.

        Args:
            customer_id: Id of the customer to delete.
            deleted_by_id: Internal id of the acting user, used for
                audit logging.

        Raises:
            CustomerNotFoundError: If the customer does not exist.
            CustomerServiceError: On unexpected persistence failure.
        """
        await self._get_existing_customer(customer_id)
        try:
            deleted = await self._customer_repository.delete(customer_id)
        except SQLAlchemyError as exc:
            await self._map_write_failure(exc, "delete_customer")
            raise  # pragma: no cover

        if not deleted:
            raise CustomerNotFoundError(f"Customer '{customer_id}' was not found during delete.")

        self._audit.emit("customer_deleted", customer_id=str(customer_id), deleted_by_id=deleted_by_id)

    async def soft_delete_customer(self, customer_id: uuid.UUID, deleted_by_id: int) -> Customer:
        """Marks a customer as inactive without removing the record.

        Args:
            customer_id: Id of the customer to soft-delete.
            deleted_by_id: Internal id of the acting user, used for
                audit logging.

        Returns:
            The updated (deactivated) ``Customer``.

        Raises:
            CustomerNotFoundError: If the customer does not exist.
            InvalidCustomerStateError: If the customer is already
                inactive.
            CustomerServiceError: On unexpected persistence failure.
        """
        existing = await self._get_existing_customer(customer_id)
        if not existing.is_active:
            raise InvalidCustomerStateError(f"Customer '{customer_id}' is already inactive.")

        try:
            customer = await self._customer_repository.soft_delete(customer_id)
        except SQLAlchemyError as exc:
            await self._map_write_failure(exc, "soft_delete_customer")
            raise  # pragma: no cover

        if customer is None:
            raise CustomerNotFoundError(f"Customer '{customer_id}' was not found during soft delete.")

        self._audit.emit("customer_soft_deleted", customer_id=str(customer_id), deleted_by_id=deleted_by_id)
        return customer

    async def restore_customer(self, customer_id: uuid.UUID, restored_by_id: int) -> Customer:
        """Restores a previously soft-deleted customer.

        Args:
            customer_id: Id of the customer to restore.
            restored_by_id: Internal id of the acting user, used for
                audit logging.

        Returns:
            The restored (active) ``Customer``.

        Raises:
            CustomerNotFoundError: If the customer does not exist.
            InvalidCustomerStateError: If the customer is already
                active.
            CustomerServiceError: On unexpected persistence failure.
        """
        existing = await self._get_existing_customer(customer_id)
        if existing.is_active:
            raise InvalidCustomerStateError(f"Customer '{customer_id}' is already active.")

        try:
            customer = await self._customer_repository.restore(customer_id)
        except SQLAlchemyError as exc:
            await self._map_write_failure(exc, "restore_customer")
            raise  # pragma: no cover

        if customer is None:
            raise CustomerNotFoundError(f"Customer '{customer_id}' was not found during restore.")

        self._audit.emit("customer_restored", customer_id=str(customer_id), restored_by_id=restored_by_id)
        return customer

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------

    async def assign_customer(
        self, customer_id: uuid.UUID, user_id: int, assigned_by_id: int
    ) -> Customer:
        """Assigns a customer to a sales agent.

        Args:
            customer_id: Id of the customer to assign.
            user_id: Internal id of the user to assign the customer to.
            assigned_by_id: Internal id of the acting user, used for
                audit logging only (the approved model has no
                "assigned_by" column, so this is not persisted).

        Returns:
            The updated ``Customer`` reflecting the new assignment.

        Raises:
            CustomerNotFoundError: If the customer does not exist.
            UserNotFoundError: If the target user does not exist.
            CustomerServiceError: On unexpected persistence failure.
        """
        await self._get_existing_customer(customer_id)
        await self._validators.assert_user_exists(user_id)

        try:
            customer = await self._customer_repository.assign_customer(customer_id, user_id)
        except SQLAlchemyError as exc:
            await self._map_write_failure(exc, "assign_customer")
            raise  # pragma: no cover

        if customer is None:
            raise CustomerNotFoundError(f"Customer '{customer_id}' was not found during assignment.")

        self._audit.emit(
            "customer_assigned",
            customer_id=str(customer_id),
            assigned_to_id=user_id,
            assigned_by_id=assigned_by_id,
        )
        return customer

    async def unassign_customer(self, customer_id: uuid.UUID, unassigned_by_id: int) -> Customer:
        """Removes the current agent assignment from a customer.

        Args:
            customer_id: Id of the customer to unassign.
            unassigned_by_id: Internal id of the acting user, used for
                audit logging.

        Returns:
            The updated (unassigned) ``Customer``.

        Raises:
            CustomerNotFoundError: If the customer does not exist.
            CustomerServiceError: On unexpected persistence failure.
        """
        await self._get_existing_customer(customer_id)

        try:
            customer = await self._customer_repository.unassign_customer(customer_id)
        except SQLAlchemyError as exc:
            await self._map_write_failure(exc, "unassign_customer")
            raise  # pragma: no cover

        if customer is None:
            raise CustomerNotFoundError(f"Customer '{customer_id}' was not found during unassignment.")

        self._audit.emit(
            "customer_unassigned", customer_id=str(customer_id), unassigned_by_id=unassigned_by_id
        )
        return customer

    # ------------------------------------------------------------------
    # Status & Follow-up
    # ------------------------------------------------------------------

    async def update_customer_status(
        self, customer_id: uuid.UUID, status: CustomerStatus, updated_by_id: int
    ) -> Customer:
        """Updates a customer's lifecycle status.

        Enum-membership validation happens at the Pydantic/DB-enum
        layer. No transition state-machine is enforced here — the
        approved model's allowed ``CustomerStatus`` transitions were
        not part of any review round; add one once those rules are
        confirmed.

        Args:
            customer_id: Id of the customer to update.
            status: The new status value.
            updated_by_id: Internal id of the acting user, used for
                audit logging only (``update_status`` on the approved
                repository does not accept/persist ``updated_by_id``).

        Returns:
            The updated ``Customer``.

        Raises:
            CustomerNotFoundError: If the customer does not exist.
            CustomerServiceError: On unexpected persistence failure.
        """
        existing = await self._get_existing_customer(customer_id)

        try:
            customer = await self._customer_repository.update_status(customer_id, status)
        except SQLAlchemyError as exc:
            await self._map_write_failure(exc, "update_customer_status")
            raise  # pragma: no cover

        if customer is None:
            raise CustomerNotFoundError(f"Customer '{customer_id}' was not found during status update.")

        self._audit.emit(
            "customer_status_updated",
            customer_id=str(customer_id),
            previous_status=getattr(existing.status, "value", existing.status),
            new_status=getattr(status, "value", status),
            updated_by_id=updated_by_id,
        )
        return customer

    async def update_followup(
        self,
        customer_id: uuid.UUID,
        *,
        next_followup_date: Optional[date],
        last_contacted_at: Optional[datetime] = None,
        updated_by_id: int,
    ) -> Customer:
        """Updates a customer's next follow-up date and/or last-contacted time.

        Args:
            customer_id: Id of the customer to update.
            next_followup_date: New next-follow-up date, or ``None`` to
                clear a scheduled follow-up.
            last_contacted_at: Optional new last-contacted timestamp,
                typically set to "now" by the caller when this call
                represents an interaction that just occurred.
            updated_by_id: Internal id of the acting user, used for
                audit logging only (``update_followup`` on the
                approved repository does not accept/persist
                ``updated_by_id``).

        Returns:
            The updated ``Customer``.

        Raises:
            CustomerNotFoundError: If the customer does not exist.
            InvalidCustomerStateError: If ``next_followup_date`` is in
                the past.
            CustomerServiceError: On unexpected persistence failure.
        """
        await self._get_existing_customer(customer_id)

        if next_followup_date is not None and next_followup_date < date.today():
            raise InvalidCustomerStateError("Follow-up date must be today or in the future.")

        try:
            customer = await self._customer_repository.update_followup(
                customer_id,
                next_followup_date=next_followup_date,
                last_contacted_at=last_contacted_at,
            )
        except SQLAlchemyError as exc:
            await self._map_write_failure(exc, "update_followup")
            raise  # pragma: no cover

        if customer is None:
            raise CustomerNotFoundError(f"Customer '{customer_id}' was not found during follow-up update.")

        self._audit.emit(
            "customer_followup_updated",
            customer_id=str(customer_id),
            next_followup_date=next_followup_date.isoformat() if next_followup_date else None,
            updated_by_id=updated_by_id,
        )
        return customer

    # ------------------------------------------------------------------
    # Export & Statistics
    # ------------------------------------------------------------------

    async def export_customers(
        self, request: CustomerExportRequest, exported_by_id: int
    ) -> list[dict[str, Any]]:
        """Prepares export-ready customer data.

        Retrieves matching customers via ``CustomerRepository.export()``
        and delegates column selection/projection to
        ``_CustomerExporter``. Note this is distinct from logging: the
        audit log entry below never contains row-level data, KYC or
        otherwise — only the column names and row count.

        Args:
            request: Export format, optional filters, and column
                selection.
            exported_by_id: Internal id of the acting user, used for
                audit logging.

        Returns:
            A list of plain dictionaries, one per customer, ready for
            the caller to serialize into ``request.export_format``.

        Raises:
            CustomerServiceError: On unexpected persistence failure.
        """
        filter_kwargs: dict[str, Any] = {}
        if request.filters is not None:
            filter_kwargs = {
                key: value
                for key, value in request.filters.model_dump().items()
                if key not in _EXPORT_UNSUPPORTED_FILTER_KEYS
            }

        try:
            customers = await self._customer_repository.export(**filter_kwargs)
        except SQLAlchemyError as exc:
            self._audit.emit("customer_export_failed", error=str(exc))
            raise CustomerServiceError("Unable to export customers.") from exc

        columns = self._exporter.resolve_columns(request)
        export_rows = self._exporter.to_rows(customers, columns)

        self._audit.emit(
            "customer_exported",
            exported_by_id=exported_by_id,
            export_format=request.export_format,
            record_count=len(export_rows),
            columns=columns,
        )
        return export_rows

    async def get_customer_statistics(
        self,
        *,
        period_start: Optional[date] = None,
        period_end: Optional[date] = None,
        top_cities_limit: int = 10,
        is_active: Optional[bool] = True,
    ) -> CustomerStatisticsResponse:
        """Retrieves aggregate customer statistics for dashboards/reporting.

        Args:
            period_start: Optional inclusive lower bound on
                ``created_at``.
            period_end: Optional inclusive upper bound on
                ``created_at``.
            top_cities_limit: Maximum number of cities to include.
            is_active: Optional active-flag filter; defaults to
                ``True``.

        Returns:
            A ``CustomerStatisticsResponse`` populated from the
            repository's aggregate query results.

        Raises:
            CustomerServiceError: On unexpected persistence failure.
        """
        try:
            stats = await self._customer_repository.get_statistics(
                period_start=period_start,
                period_end=period_end,
                top_cities_limit=top_cities_limit,
                is_active=is_active,
            )
        except SQLAlchemyError as exc:
            self._audit.emit("customer_statistics_failed", error=str(exc))
            raise CustomerServiceError("Unable to retrieve customer statistics.") from exc

        return CustomerStatisticsResponse(**stats)