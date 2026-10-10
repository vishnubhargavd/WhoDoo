import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ledger import AuditLog


def record(
    session: AsyncSession,
    *,
    club_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    details: dict[str, Any] | None = None,
) -> None:
    """Queue an audit row in the caller's transaction, so it commits or rolls back with the change."""
    session.add(
        AuditLog(
            club_id=club_id,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
        )
    )
