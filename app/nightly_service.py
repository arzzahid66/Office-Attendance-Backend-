from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attendance_service import get_or_create_attendance_day, has_approved_wfh
from app.models import Heartbeat, RandomCheck, User
from app.timeutil import to_local


async def sweep_missed_checks(db: AsyncSession, day: date) -> int:
    """End-of-day safety net: anything still pending for the day is missed."""
    result = await db.execute(select(RandomCheck).where(RandomCheck.date == day, RandomCheck.result == "pending"))
    checks = result.scalars().all()
    for check in checks:
        check.result = "missed"
    await db.commit()
    return len(checks)


async def resolve_day(db: AsyncSession, day: date) -> int:
    """Nightly resolution: 3+ passed checks + a check-in keeps the day's mode (verified).
    Some activity but fewer than 3 passed checks is flagged for admin review (never
    auto-absent). Zero activity with no approved WFH for the day is marked absent."""
    employees_result = await db.execute(select(User).where(User.role == "employee", User.status == "active"))
    employees = employees_result.scalars().all()

    resolved = 0
    for emp in employees:
        checks_result = await db.execute(select(RandomCheck).where(RandomCheck.user_id == emp.id, RandomCheck.date == day))
        checks = checks_result.scalars().all()
        passed_checks = sum(1 for c in checks if c.result == "passed")
        total_checks = len(checks)

        wfh_approved = await has_approved_wfh(db, emp.id, day)
        attendance = await get_or_create_attendance_day(db, emp.id, day)
        has_check_in = attendance.check_in is not None
        zero_activity = not has_check_in and passed_checks == 0 and attendance.check_out is None

        if has_check_in and passed_checks >= 3:
            pass  # verified: keep existing mode (office/wfh) as set during check-in
        elif zero_activity and not wfh_approved:
            attendance.mode = "absent"
        else:
            attendance.mode = "flagged"

        attendance.passed_checks = passed_checks
        attendance.total_checks = total_checks
        resolved += 1

    await db.commit()
    return resolved


async def apply_checkout_fallback(db: AsyncSession, day: date) -> int:
    """If an employee never pressed check-out, use the last matched heartbeat's time instead."""
    from app.models import AttendanceDay, Device

    result = await db.execute(select(AttendanceDay).where(AttendanceDay.date == day, AttendanceDay.check_out.is_(None)))
    days = result.scalars().all()

    updated = 0
    for attendance in days:
        devices_result = await db.execute(select(Device.id).where(Device.user_id == attendance.user_id))
        device_ids = [d for (d,) in devices_result.all()]
        if not device_ids:
            continue
        hb_result = await db.execute(
            select(Heartbeat)
            .where(Heartbeat.device_id.in_(device_ids), Heartbeat.ip_matched.is_(True))
            .order_by(Heartbeat.ts.desc())
            .limit(1)
        )
        last_matched = hb_result.scalar_one_or_none()
        if last_matched is not None and to_local(last_matched.ts).date() == day:
            attendance.check_out = last_matched.ts
            updated += 1

    await db.commit()
    return updated
