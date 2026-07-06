from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Device, User
from app.security import decode_token, hash_device_token

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_token(credentials.credentials)
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    if payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token type")
    user = await db.get(User, int(payload["sub"]))
    if user is None or user.status == "disabled":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or disabled")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


async def require_active_employee(user: User = Depends(get_current_user)) -> User:
    if user.role != "employee":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Employee access required")
    if user.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account not active")
    return user


async def get_current_device(
    x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
    user: User = Depends(require_active_employee),
    db: AsyncSession = Depends(get_db),
) -> Device:
    if not x_device_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-Device-Token header")
    token_hash = hash_device_token(x_device_token)
    result = await db.execute(select(Device).where(Device.token_hash == token_hash, Device.user_id == user.id))
    device = result.scalar_one_or_none()
    if device is None or device.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked device")
    return device


def require_dev_mode():
    from app.config import get_settings

    if not get_settings().dev_mode:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
