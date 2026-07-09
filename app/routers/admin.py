from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log_action
from app.database import get_db
from app.deps import require_admin
from app.models import AttendanceDay, AuditLog, Device, Roster, User
from app.schemas import (
    DashboardEntryOut,
    DeviceOut,
    EmployeeOut,
    FlaggedDayOut,
    FlaggedResolveIn,
    UserDecisionIn,
    VerifyDecisionIn,
)
from app.timeutil import now_utc, office_today, today_local

router = APIRouter()


async def _get_employee(db: AsyncSession, employee_id: int) -> User:
    employee = await db.get(User, employee_id)
    if employee is None or employee.role != "employee":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    return employee


async def _active_device_count(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(
        select(func.count(Device.id)).where(Device.user_id == user_id, Device.status == "active")
    )
    return result.scalar_one()


def _employee_out(employee: User, active_device_count: int = 0) -> EmployeeOut:
    return EmployeeOut(
        id=employee.id,
        name=employee.name,
        email=employee.email,
        phone=employee.phone,
        status=employee.status,
        department=employee.department,
        job_title=employee.job_title,
        city=employee.city,
        requested_roster_id=employee.requested_roster_id,
        assigned_roster_id=employee.assigned_roster_id,
        admin_feedback=employee.admin_feedback,
        created_at=employee.created_at,
        active_device_count=active_device_count,
    )


async def _require_active_roster(db: AsyncSession, roster_id: int | None) -> Roster:
    if roster_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "assigned_roster_id is required when approving: an approved user with no roster "
            "cannot check in (403 'Roster not assigned').",
        )
    roster = await db.get(Roster, roster_id)
    if roster is None or not roster.active:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "assigned_roster_id must reference an active roster"
        )
    return roster


@router.get("/employees", response_model=list[EmployeeOut])
async def list_employees(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    result = await db.execute(select(User).where(User.role == "employee").order_by(User.created_at.desc()))
    employees = result.scalars().all()

    counts_result = await db.execute(
        select(Device.user_id, func.count(Device.id)).where(Device.status == "active").group_by(Device.user_id)
    )
    counts = dict(counts_result.all())

    return [_employee_out(e, counts.get(e.id, 0)) for e in employees]


@router.post("/users/{user_id}/decision", response_model=EmployeeOut)
async def decide_user(
    user_id: int,
    payload: UserDecisionIn,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Approve or reject a pending signup. Admin may grant a roster different from the
    one the employee requested. Only PENDING users can be decided — re-deciding an
    already-decided user is a 409, never a silent second approval."""
    employee = await _get_employee(db, user_id)

    if employee.status != "pending":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"User is already '{employee.status}'. Only a pending signup can be decided.",
        )

    if payload.action == "approve":
        roster = await _require_active_roster(db, payload.assigned_roster_id)
        employee.status = "active"
        employee.assigned_roster_id = roster.id
        employee.approved_by = admin.id
        employee.approved_at = now_utc()
        employee.admin_feedback = payload.feedback
        await log_action(
            db,
            admin.id,
            "user_approved",
            {
                "user_id": employee.id,
                "email": employee.email,
                "requested_roster_id": employee.requested_roster_id,
                "assigned_roster_id": roster.id,
                "feedback": payload.feedback,
            },
        )
    else:
        employee.status = "rejected"
        employee.assigned_roster_id = None
        employee.admin_feedback = payload.feedback
        # approved_by/approved_at stay NULL on a rejection; audit_logs records who rejected.
        await log_action(
            db,
            admin.id,
            "user_rejected",
            {
                "user_id": employee.id,
                "email": employee.email,
                "requested_roster_id": employee.requested_roster_id,
                "feedback": payload.feedback,
            },
        )

    await db.commit()
    await db.refresh(employee)
    return _employee_out(employee, await _active_device_count(db, employee.id))


@router.post("/employees/{employee_id}/disable", response_model=EmployeeOut)
async def disable_employee(employee_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    employee = await _get_employee(db, employee_id)
    employee.status = "disabled"
    await log_action(db, admin.id, "employee_disabled", {"user_id": employee_id})
    await db.commit()
    await db.refresh(employee)
    return _employee_out(employee, await _active_device_count(db, employee_id))


@router.post("/employees/{employee_id}/enable", response_model=EmployeeOut)
async def enable_employee(employee_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    employee = await _get_employee(db, employee_id)
    # Same trap as approval: an active user with no roster gets a 403 on every check-in.
    if employee.assigned_roster_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot enable a user with no assigned roster. Assign a roster first.",
        )
    employee.status = "active"
    await log_action(db, admin.id, "employee_enabled", {"user_id": employee_id})
    await db.commit()
    await db.refresh(employee)
    return _employee_out(employee, await _active_device_count(db, employee_id))


@router.delete("/employees/{employee_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_employee(employee_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    """Permanently removes the employee. Devices, heartbeats, attendance days and check-in
    attempts go with them (ON DELETE CASCADE) — this is not reversible. Disable instead if
    the attendance history still needs to be reportable."""
    employee = await _get_employee(db, employee_id)

    # Snapshot the identity into the audit row: after the delete, user_id points at nothing.
    await log_action(
        db,
        admin.id,
        "employee_deleted",
        {"user_id": employee.id, "email": employee.email, "name": employee.name},
    )

    # audit_logs.actor_user_id has no ON DELETE rule, and employees are actors on their own
    # rows (signup, device_registered). Detach them so the trail survives the user.
    await db.execute(
        update(AuditLog).where(AuditLog.actor_user_id == employee_id).values(actor_user_id=None)
    )

    await db.delete(employee)
    await db.commit()


@router.get("/employees/{employee_id}/devices", response_model=list[DeviceOut])
async def list_employee_devices(employee_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    await _get_employee(db, employee_id)
    result = await db.execute(select(Device).where(Device.user_id == employee_id).order_by(Device.created_at.desc()))
    return result.scalars().all()


@router.post("/employees/{employee_id}/devices/{device_id}/revoke", response_model=DeviceOut)
async def revoke_device(
    employee_id: int, device_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    device = await db.get(Device, device_id)
    if device is None or device.user_id != employee_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device not found")
    device.status = "revoked"
    await log_action(db, admin.id, "device_revoked_by_admin", {"device_id": device_id, "user_id": employee_id})
    await db.commit()
    await db.refresh(device)
    return device


# --------------------------------------------------------------------------- live dashboard
@router.get("/dashboard", response_model=list[DashboardEntryOut])
async def dashboard(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Today's live view. `date` is resolved per employee in THEIR office's timezone."""
    employees = (
        await db.execute(
            select(User).where(User.role == "employee", User.status == "active").order_by(User.name)
        )
    ).scalars().all()

    entries: list[DashboardEntryOut] = []
    for emp in employees:
        roster = emp.assigned_roster
        local_date = office_today(roster.office) if roster else today_local()
        day = (
            await db.execute(
                select(AttendanceDay).where(AttendanceDay.user_id == emp.id, AttendanceDay.date == local_date)
            )
        ).scalar_one_or_none()

        entries.append(
            DashboardEntryOut(
                user_id=emp.id,
                name=emp.name,
                email=emp.email,
                roster_name=roster.name if roster else None,
                shift_start=roster.start_time if roster else None,
                shift_end=roster.end_time if roster else None,
                day_id=day.id if day else None,
                status=day.status if day else None,
                method=day.method if day else None,
                verification=day.verification if day else None,
                check_in=day.check_in if day else None,
                check_out=day.check_out if day else None,
                last_seen=day.last_seen if day else None,
                late_minutes=day.late_minutes if day else 0,
            )
        )
    return entries


@router.post("/attendance/{day_id}/verify", response_model=FlaggedDayOut)
async def verify_gps_day(
    day_id: int, payload: VerifyDecisionIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    """Resolve a GPS check-in that the system could only mark `gps_pending` (the browser
    cannot detect mock locations, so GPS is never auto-trusted)."""
    day = await db.get(AttendanceDay, day_id)
    if day is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attendance day not found")
    if day.verification != "gps_pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "This day is not awaiting GPS verification")

    if payload.action == "approve":
        day.verification = "verified"
    else:  # query -> send to flagged for follow-up, keep the GPS evidence
        day.status = "flagged"
    day.admin_note = payload.note
    day.resolved_by = admin.id
    await log_action(db, admin.id, f"gps_{payload.action}", {"day_id": day_id, "note": payload.note})
    await db.commit()
    await db.refresh(day)
    user = await db.get(User, day.user_id)
    return _flagged_out(day, user)


# --------------------------------------------------------------------------- flagged review
def _flagged_out(day: AttendanceDay, user: User) -> FlaggedDayOut:
    return FlaggedDayOut(
        id=day.id,
        user_id=user.id,
        name=user.name,
        email=user.email,
        date=day.date,
        status=day.status,
        method=day.method,
        verification=day.verification,
        check_in=day.check_in,
        check_out=day.check_out,
        late_minutes=day.late_minutes,
        admin_note=day.admin_note,
    )


@router.get("/flagged-days", response_model=list[FlaggedDayOut])
async def flagged_days(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    rows = (
        await db.execute(
            select(AttendanceDay, User)
            .join(User, User.id == AttendanceDay.user_id)
            .where(AttendanceDay.status == "flagged")
            .order_by(AttendanceDay.date.desc())
        )
    ).all()
    return [_flagged_out(day, user) for day, user in rows]


@router.post("/flagged-days/{day_id}/resolve", response_model=FlaggedDayOut)
async def resolve_flagged_day(
    day_id: int, payload: FlaggedResolveIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    """The ONLY path to 'absent' or 'leave' — a human decides, never the nightly job."""
    day = await db.get(AttendanceDay, day_id)
    if day is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attendance day not found")
    day.status = payload.status
    day.admin_note = payload.note
    day.resolved_by = admin.id
    await log_action(
        db, admin.id, "flagged_day_resolved", {"day_id": day_id, "status": payload.status, "note": payload.note}
    )
    await db.commit()
    await db.refresh(day)
    user = await db.get(User, day.user_id)
    return _flagged_out(day, user)
