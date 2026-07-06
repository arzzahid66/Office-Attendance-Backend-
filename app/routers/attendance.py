from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attendance_service import get_attendance_day, get_or_create_attendance_day, has_approved_wfh, ip_matches_office
from app.database import get_db
from app.deps import get_current_device, require_active_employee
from app.ip_utils import get_client_ip
from app.models import AttendanceDay, Device, Heartbeat, RandomCheck, User
from app.schemas import AttendanceDayOut, RandomCheckOut, TodayOut
from app.timeutil import now_utc, today_local

router = APIRouter()


@router.get("/today", response_model=TodayOut)
async def today(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_active_employee),
):
    ip = get_client_ip(request)
    matched = await ip_matches_office(db, ip)
    day = await get_attendance_day(db, user.id, today_local())

    checks_result = await db.execute(
        select(RandomCheck).where(RandomCheck.user_id == user.id, RandomCheck.date == today_local())
        .order_by(RandomCheck.scheduled_at.asc())
    )
    checks = checks_result.scalars().all()

    return TodayOut(
        detected_ip=ip,
        ip_matched=matched,
        attendance=AttendanceDayOut.model_validate(day) if day else None,
        checks=[RandomCheckOut.model_validate(c) for c in checks],
    )


@router.get("/history", response_model=list[AttendanceDayOut])
async def history(db: AsyncSession = Depends(get_db), user: User = Depends(require_active_employee)):
    cutoff = today_local() - timedelta(days=30)
    result = await db.execute(
        select(AttendanceDay)
        .where(AttendanceDay.user_id == user.id, AttendanceDay.date >= cutoff)
        .order_by(AttendanceDay.date.desc())
    )
    return result.scalars().all()


@router.post("/check-in", response_model=AttendanceDayOut)
async def check_in(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_active_employee),
    device: Device = Depends(get_current_device),
):
    ip = get_client_ip(request)
    matched = await ip_matches_office(db, ip)
    today_date = today_local()
    wfh_approved = await has_approved_wfh(db, user.id, today_date)

    if not matched and not wfh_approved:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Your IP {ip} is not a registered office network",
        )

    day = await get_or_create_attendance_day(db, user.id, today_date)
    now = now_utc()
    if day.check_in is None:
        day.check_in = now
    day.mode = "office" if matched else "wfh"

    device.last_seen_at = now
    db.add(Heartbeat(device_id=device.id, ts=now, source_ip=ip, ip_matched=matched))

    await db.commit()
    await db.refresh(day)
    return day


@router.post("/heartbeat")
async def heartbeat(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_active_employee),
    device: Device = Depends(get_current_device),
):
    ip = get_client_ip(request)
    matched = await ip_matches_office(db, ip)
    now = now_utc()
    today_date = today_local()

    db.add(Heartbeat(device_id=device.id, ts=now, source_ip=ip, ip_matched=matched))
    device.last_seen_at = now

    day = await get_or_create_attendance_day(db, user.id, today_date)
    is_wfh_day = day.mode == "wfh"
    if matched or is_wfh_day:
        day.check_out = now

    await db.commit()
    return {"ip": ip, "matched": matched}


@router.post("/check-out", response_model=AttendanceDayOut)
async def check_out(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_active_employee),
    device: Device = Depends(get_current_device),
):
    today_date = today_local()
    day = await get_attendance_day(db, user.id, today_date)
    if day is None or day.check_in is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You haven't checked in today")

    now = now_utc()
    day.check_out = now
    device.last_seen_at = now

    ip = get_client_ip(request)
    matched = await ip_matches_office(db, ip)
    db.add(Heartbeat(device_id=device.id, ts=now, source_ip=ip, ip_matched=matched))

    await db.commit()
    await db.refresh(day)
    return day
