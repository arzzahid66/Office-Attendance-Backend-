from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends, HTTPException, status

from app.audit import log_action
from app.database import get_db
from app.deps import require_admin
from app.models import AttendanceDay, Device, User
from app.schemas import (
    DashboardEntryOut,
    DeviceOut,
    EmployeeOut,
    FlaggedDayOut,
    FlaggedResolveIn,
)
from app.timeutil import today_local

router = APIRouter()


@router.get("/employees", response_model=list[EmployeeOut])
async def list_employees(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    result = await db.execute(select(User).where(User.role == "employee").order_by(User.created_at.desc()))
    employees = result.scalars().all()

    counts_result = await db.execute(
        select(Device.user_id, func.count(Device.id)).where(Device.status == "active").group_by(Device.user_id)
    )
    counts = dict(counts_result.all())

    return [
        EmployeeOut(
            id=e.id,
            name=e.name,
            email=e.email,
            status=e.status,
            department=e.department,
            job_title=e.job_title,
            city=e.city,
            created_at=e.created_at,
            active_device_count=counts.get(e.id, 0),
        )
        for e in employees
    ]


def _employee_out(employee: User, active_device_count: int = 0) -> EmployeeOut:
    return EmployeeOut(
        id=employee.id,
        name=employee.name,
        email=employee.email,
        status=employee.status,
        department=employee.department,
        job_title=employee.job_title,
        city=employee.city,
        created_at=employee.created_at,
        active_device_count=active_device_count,
    )


@router.post("/employees/{employee_id}/approve", response_model=EmployeeOut)
async def approve_employee(employee_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    employee = await _get_employee(db, employee_id)
    employee.status = "active"
    await log_action(db, admin.id, "employee_approved", f"user_id={employee_id}")
    await db.commit()
    return _employee_out(employee)


@router.post("/employees/{employee_id}/disable", response_model=EmployeeOut)
async def disable_employee(employee_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    employee = await _get_employee(db, employee_id)
    employee.status = "disabled"
    await log_action(db, admin.id, "employee_disabled", f"user_id={employee_id}")
    await db.commit()
    return _employee_out(employee)


@router.post("/employees/{employee_id}/enable", response_model=EmployeeOut)
async def enable_employee(employee_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    employee = await _get_employee(db, employee_id)
    employee.status = "active"
    await log_action(db, admin.id, "employee_enabled", f"user_id={employee_id}")
    await db.commit()
    return _employee_out(employee)


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
    await log_action(db, admin.id, "device_revoked_by_admin", f"device_id={device_id} user_id={employee_id}")
    await db.commit()
    await db.refresh(device)
    return device


@router.get("/dashboard", response_model=list[DashboardEntryOut])
async def dashboard(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    today_date = today_local()

    employees_result = await db.execute(select(User).where(User.role == "employee", User.status == "active"))
    employees = employees_result.scalars().all()

    days_result = await db.execute(select(AttendanceDay).where(AttendanceDay.date == today_date))
    days_by_user = {d.user_id: d for d in days_result.scalars()}

    last_seen_result = await db.execute(
        select(Device.user_id, func.max(Device.last_seen_at)).where(Device.status == "active").group_by(Device.user_id)
    )
    last_seen_by_user = dict(last_seen_result.all())

    entries = []
    for emp in employees:
        day = days_by_user.get(emp.id)
        entries.append(
            DashboardEntryOut(
                user_id=emp.id,
                name=emp.name,
                email=emp.email,
                mode=day.mode if day else "pending",
                check_in=day.check_in if day else None,
                check_out=day.check_out if day else None,
                source_ip=day.source_ip if day else None,
                location=day.location if day else None,
                last_heartbeat_at=last_seen_by_user.get(emp.id),
            )
        )
    return entries


@router.get("/flagged-days", response_model=list[FlaggedDayOut])
async def flagged_days(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    result = await db.execute(
        select(AttendanceDay, User)
        .join(User, User.id == AttendanceDay.user_id)
        .where(AttendanceDay.mode == "flagged")
        .order_by(AttendanceDay.date.desc())
    )
    return [
        FlaggedDayOut(
            id=day.id,
            user_id=user.id,
            name=user.name,
            email=user.email,
            date=day.date,
            mode=day.mode,
            check_in=day.check_in,
            check_out=day.check_out,
            passed_checks=day.passed_checks,
            total_checks=day.total_checks,
            note=day.note,
        )
        for day, user in result.all()
    ]


@router.post("/flagged-days/{day_id}/resolve", response_model=FlaggedDayOut)
async def resolve_flagged_day(
    day_id: int, payload: FlaggedResolveIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    day = await db.get(AttendanceDay, day_id)
    if day is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attendance day not found")
    day.mode = payload.mode
    day.note = payload.note
    await log_action(db, admin.id, "flagged_day_resolved", f"day_id={day_id} mode={payload.mode}")
    await db.commit()
    await db.refresh(day)
    user = await db.get(User, day.user_id)
    return FlaggedDayOut(
        id=day.id,
        user_id=user.id,
        name=user.name,
        email=user.email,
        date=day.date,
        mode=day.mode,
        check_in=day.check_in,
        check_out=day.check_out,
        passed_checks=day.passed_checks,
        total_checks=day.total_checks,
        note=day.note,
    )


async def _get_employee(db: AsyncSession, employee_id: int) -> User:
    employee = await db.get(User, employee_id)
    if employee is None or employee.role != "employee":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    return employee
