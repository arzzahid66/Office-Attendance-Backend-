from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attendance_service import get_attendance_day, has_approved_wfh, ip_matches_office
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_device, require_active_employee
from app.ip_utils import get_client_ip
from app.models import Device, RandomCheck, User
from app.schemas import RandomCheckOut
from app.timeutil import now_utc

router = APIRouter()
settings = get_settings()


@router.get("/pending", response_model=list[RandomCheckOut])
async def pending_checks(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_active_employee),
):
    now = now_utc()
    result = await db.execute(select(RandomCheck).where(RandomCheck.user_id == user.id, RandomCheck.result == "pending"))
    checks = result.scalars().all()

    window = timedelta(minutes=settings.check_response_window_minutes)
    visible = []
    for check in checks:
        if now > check.scheduled_at + window:
            check.result = "missed"
            continue
        if now >= check.scheduled_at:
            visible.append(check)

    await db.commit()
    return visible


@router.post("/{check_id}/respond", response_model=RandomCheckOut)
async def respond_check(
    check_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_active_employee),
    device: Device = Depends(get_current_device),
):
    check = await db.get(RandomCheck, check_id)
    if check is None or check.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Check not found")
    if check.result != "pending":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This check has already been resolved")

    now = now_utc()
    window = timedelta(minutes=settings.check_response_window_minutes)
    if now > check.scheduled_at + window:
        check.result = "missed"
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This check window has expired")

    ip = get_client_ip(request)
    matched = await ip_matches_office(db, ip)
    is_wfh = await has_approved_wfh(db, user.id, check.date)

    check.responded_at = now
    check.source_ip = ip
    check.result = "passed" if (is_wfh or matched) else "missed"

    device.last_seen_at = now
    await db.commit()
    await db.refresh(check)
    return check
