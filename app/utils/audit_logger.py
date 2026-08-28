"""Reusable audit-logging helpers for use across every domain module.

Every domain module (Customer, Lead, Property, Booking, Payment,
Dashboard, Report, AI, Notification, etc.) should record significant
state changes and sensitive operations through these helpers rather
than constructing :class:`~app.schemas.audit_log.AuditLogCreate`
payloads or invoking :class:`~app.services.audit_log_service.AuditLogService`
directly. This keeps audit-writing call sites thin, consistent, and
easy to evolve.

Example:
    ```python
    from app.utils.audit_logger import log_update

    await log_update(
        db=session,
        user_id=current_user.id,
        module="customer",
        entity_type="Customer",
        entity_id=str(customer.id),
        description=f"Updated customer {customer.id}",
        old_data=old_snapshot,
        new_data=new_snapshot,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
        request_id=request.headers.get("x-request-id"),
    )
    ```
"""

import uuid
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditAction, AuditSeverity, AuditStatus
from app.repositories.audit_log_repository import AuditLogRepository
from app.schemas.audit_log import AuditLogCreate, AuditLogResponse
from app.services.audit_log_service import AuditLogService

__all__ = [
    "log_create",
    "log_update",
    "log_delete",
    "log_login",
    "log_logout",
    "log_export",
    "log_import",
    "log_assign",
    "log_unassign",
    "log_upload",
    "log_download",
    "log_send",
    "log_generate",
    "log_custom",
]


def _build_service(db: AsyncSession) -> AuditLogService:
    """Constructs an :class:`AuditLogService` bound to the given session.

    Args:
        db: The active asynchronous SQLAlchemy session.

    Returns:
        AuditLogService: A service instance ready to persist audit entries.
    """
    repository = AuditLogRepository(db)
    return AuditLogService(repository)


async def log_custom(
    *,
    db: AsyncSession,
    module: str,
    action: AuditAction,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    old_data: Optional[dict[str, Any]] = None,
    new_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit log entry for an arbitrary, caller-specified action.

    This is the common implementation underlying every action-specific
    helper (`log_create`, `log_update`, etc.) and can also be called
    directly for actions that don't map cleanly onto one of them.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module (e.g. ``"customer"``).
        action: The action performed.
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, or ``None`` for
            system-initiated or unauthenticated events.
        entity_type: Name of the affected entity/table, if applicable.
        entity_id: Primary key of the affected entity, if applicable.
        old_data: Snapshot of entity state prior to the action.
        new_data: Snapshot of entity state after the action.
        severity: Severity classification of the event.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.

    Raises:
        ValidationException: If any field fails domain validation.
        NotFoundException: If ``user_id`` is supplied but does not
            correspond to an existing user.
    """
    service = _build_service(db)
    payload = AuditLogCreate(
        user_id=user_id,
        module=module,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        description=description,
        old_data=old_data,
        new_data=new_data,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
        status=status,
        severity=severity,
    )
    return await service.create_log(payload)


async def log_create(
    *,
    db: AsyncSession,
    module: str,
    entity_type: str,
    entity_id: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    new_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for the creation of a new entity.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module.
        entity_type: Name of the created entity/table.
        entity_id: Primary key of the newly created entity.
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, if any.
        new_data: Snapshot of the entity state as created.
        severity: Severity classification of the event.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.CREATE,
        description=description,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        new_data=new_data,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_update(
    *,
    db: AsyncSession,
    module: str,
    entity_type: str,
    entity_id: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    old_data: Optional[dict[str, Any]] = None,
    new_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for the modification of an existing entity.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module.
        entity_type: Name of the updated entity/table.
        entity_id: Primary key of the updated entity.
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, if any.
        old_data: Snapshot of the entity state prior to the update.
        new_data: Snapshot of the entity state after the update.
        severity: Severity classification of the event.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.UPDATE,
        description=description,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        old_data=old_data,
        new_data=new_data,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_delete(
    *,
    db: AsyncSession,
    module: str,
    entity_type: str,
    entity_id: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    old_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.MEDIUM,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for the deletion of an entity.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module.
        entity_type: Name of the deleted entity/table.
        entity_id: Primary key of the deleted entity.
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, if any.
        old_data: Snapshot of the entity state prior to deletion.
        severity: Severity classification of the event. Defaults to
            ``MEDIUM`` since deletions are inherently more sensitive
            than routine creates/updates.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.DELETE,
        description=description,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        old_data=old_data,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_login(
    *,
    db: AsyncSession,
    module: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for an authentication (login) attempt.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning module (typically ``"auth"``).
        description: Human-readable summary of the event.
        user_id: Identifier of the authenticating user, if known. May be
            ``None`` for failed attempts against an unrecognized identifier.
        severity: Severity classification of the event. Callers should
            escalate to ``HIGH`` or ``CRITICAL`` for suspicious or
            repeated failed attempts.
        status: Outcome status of the login attempt.
        ip_address: Origin IP address of the login request.
        user_agent: User agent string of the login client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.LOGIN,
        description=description,
        user_id=user_id,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_logout(
    *,
    db: AsyncSession,
    module: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for a session termination (logout) event.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning module (typically ``"auth"``).
        description: Human-readable summary of the event.
        user_id: Identifier of the logging-out user, if any.
        severity: Severity classification of the event.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the logout request.
        user_agent: User agent string of the logout client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.LOGOUT,
        description=description,
        user_id=user_id,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_export(
    *,
    db: AsyncSession,
    module: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    new_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.MEDIUM,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for a data export operation.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module.
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, if any.
        entity_type: Name of the exported entity/report type, if applicable.
        entity_id: Identifier of the export job or affected entity, if any.
        new_data: Metadata describing the export (e.g. row count, format,
            applied filters).
        severity: Severity classification of the event. Defaults to
            ``MEDIUM`` since bulk data export is a sensitive operation.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.EXPORT,
        description=description,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        new_data=new_data,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_import(
    *,
    db: AsyncSession,
    module: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    new_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.MEDIUM,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for a data import operation.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module.
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, if any.
        entity_type: Name of the imported entity type, if applicable.
        entity_id: Identifier of the import job or affected entity, if any.
        new_data: Metadata describing the import (e.g. row count, source,
            validation results).
        severity: Severity classification of the event. Defaults to
            ``MEDIUM`` since bulk data import is a sensitive operation.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.IMPORT,
        description=description,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        new_data=new_data,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_assign(
    *,
    db: AsyncSession,
    module: str,
    entity_type: str,
    entity_id: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    old_data: Optional[dict[str, Any]] = None,
    new_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for assigning an entity (e.g. a Lead) to a user or team.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module.
        entity_type: Name of the assigned entity/table.
        entity_id: Primary key of the assigned entity.
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, if any.
        old_data: Snapshot of assignment state prior to the change.
        new_data: Snapshot of assignment state after the change.
        severity: Severity classification of the event.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.ASSIGN,
        description=description,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        old_data=old_data,
        new_data=new_data,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_unassign(
    *,
    db: AsyncSession,
    module: str,
    entity_type: str,
    entity_id: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    old_data: Optional[dict[str, Any]] = None,
    new_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for unassigning an entity from a user or team.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module.
        entity_type: Name of the unassigned entity/table.
        entity_id: Primary key of the unassigned entity.
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, if any.
        old_data: Snapshot of assignment state prior to the change.
        new_data: Snapshot of assignment state after the change.
        severity: Severity classification of the event.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.UNASSIGN,
        description=description,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        old_data=old_data,
        new_data=new_data,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_upload(
    *,
    db: AsyncSession,
    module: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    new_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for a file or document upload.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module.
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, if any.
        entity_type: Name of the entity the file is attached to, if any.
        entity_id: Primary key of the entity the file is attached to, if any.
        new_data: Metadata describing the uploaded file (e.g. filename,
            size, content type, storage path).
        severity: Severity classification of the event.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.UPLOAD,
        description=description,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        new_data=new_data,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_download(
    *,
    db: AsyncSession,
    module: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    new_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for a file or document download.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module.
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, if any.
        entity_type: Name of the entity the file is attached to, if any.
        entity_id: Primary key of the entity the file is attached to, if any.
        new_data: Metadata describing the downloaded file (e.g. filename,
            size, content type).
        severity: Severity classification of the event.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.DOWNLOAD,
        description=description,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        new_data=new_data,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_send(
    *,
    db: AsyncSession,
    module: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    new_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for sending a message, email, or notification.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module (e.g. ``"notification"``).
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, if any.
        entity_type: Name of the entity the communication relates to, if any.
        entity_id: Primary key of the related entity, if any.
        new_data: Metadata describing what was sent (e.g. channel,
            recipient, template id).
        severity: Severity classification of the event.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.SEND,
        description=description,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        new_data=new_data,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )


async def log_generate(
    *,
    db: AsyncSession,
    module: str,
    description: str,
    user_id: Optional[uuid.UUID] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    new_data: Optional[dict[str, Any]] = None,
    severity: AuditSeverity = AuditSeverity.LOW,
    status: AuditStatus = AuditStatus.SUCCESS,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditLogResponse:
    """Records an audit entry for generating a derived artifact.

    Suitable for AI-generated content, generated reports, generated
    documents, or any other derived output produced by the system on
    behalf of a user.

    Args:
        db: The active asynchronous SQLAlchemy session.
        module: Name of the owning domain module (e.g. ``"report"``, ``"ai"``).
        description: Human-readable summary of the event.
        user_id: Identifier of the acting user, if any.
        entity_type: Name of the generated artifact type, if applicable.
        entity_id: Identifier of the generated artifact, if any.
        new_data: Metadata describing the generated artifact (e.g. model
            used, parameters, output reference).
        severity: Severity classification of the event.
        status: Outcome status of the operation.
        ip_address: Origin IP address of the triggering request.
        user_agent: User agent string of the triggering client.
        request_id: Correlation/trace identifier for the request.

    Returns:
        AuditLogResponse: The persisted audit log entry.
    """
    return await log_custom(
        db=db,
        module=module,
        action=AuditAction.GENERATE,
        description=description,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        new_data=new_data,
        severity=severity,
        status=status,
        ip_address=ip_address,
        user_agent=user_agent,
        request_id=request_id,
    )