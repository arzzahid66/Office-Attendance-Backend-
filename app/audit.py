from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def log_action(
    db: AsyncSession, actor_user_id: int | None, action: str, detail: dict[str, Any] | None = None
) -> None:
    """Appends an audit row. `detail` is structured JSONB (not free text) — pass a dict.
    Does not commit; the caller owns the transaction."""
    db.add(AuditLog(actor_user_id=actor_user_id, action=action, detail=detail))
