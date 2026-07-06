from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import User
from app.security import hash_password

settings = get_settings()


async def ensure_admin_user(db: AsyncSession) -> None:
    """Upserts the single hardcoded admin account from ADMIN_EMAIL/ADMIN_PASSWORD on every
    startup, so env var changes to the admin password take effect without a manual migration."""
    email = settings.admin_email.lower()
    result = await db.execute(select(User).where(User.email == email))
    admin = result.scalar_one_or_none()
    if admin is None:
        admin = User(
            name="Admin",
            email=email,
            password_hash=hash_password(settings.admin_password),
            role="admin",
            status="active",
        )
        db.add(admin)
    else:
        admin.role = "admin"
        admin.status = "active"
        admin.password_hash = hash_password(settings.admin_password)
    await db.commit()
