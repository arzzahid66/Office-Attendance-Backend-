from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def log_action(db: AsyncSession, user_id: int | None, action: str, detail: str | None = None) -> None:
    db.add(AuditLog(user_id=user_id, action=action, detail=detail))
