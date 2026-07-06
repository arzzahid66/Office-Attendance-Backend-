from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log_action
from app.config import get_settings
from app.models import Device, User
from app.security import generate_device_token, hash_device_token
from app.timeutil import now_utc

settings = get_settings()


async def register_device(db: AsyncSession, user: User, platform: str | None) -> str:
    """Creates a new device for the user, evicting the oldest active device if the
    per-user cap is already reached. Returns the raw (unhashed) device token."""
    result = await db.execute(
        select(Device)
        .where(Device.user_id == user.id, Device.status == "active")
        .order_by(Device.created_at.asc())
    )
    active_devices = list(result.scalars())

    if len(active_devices) >= settings.max_devices_per_user:
        oldest = active_devices[0]
        oldest.status = "revoked"
        await log_action(
            db,
            user.id,
            "device_auto_revoked",
            f"Device {oldest.id} auto-revoked: max {settings.max_devices_per_user} devices reached",
        )

    raw_token = generate_device_token()
    device = Device(
        user_id=user.id,
        token_hash=hash_device_token(raw_token),
        platform=platform,
        status="active",
        last_seen_at=now_utc(),
    )
    db.add(device)
    await log_action(db, user.id, "device_registered", platform)
    return raw_token


async def find_active_device(db: AsyncSession, user: User, raw_token: str) -> Device | None:
    result = await db.execute(
        select(Device).where(
            Device.user_id == user.id,
            Device.token_hash == hash_device_token(raw_token),
            Device.status == "active",
        )
    )
    return result.scalar_one_or_none()
