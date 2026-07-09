import csv
import io
from calendar import monthrange
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_admin
from app.models import AttendanceDay, User
from app.schemas import MonthlyReportRow

router = APIRouter()


def _month_bounds(month: str) -> tuple[date, date]:
    try:
        year_s, month_s = month.split("-")
        year, mon = int(year_s), int(month_s)
    except (ValueError, AttributeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "month must be in YYYY-MM format")
    start = date(year, mon, 1)
    end = date(year, mon, monthrange(year, mon)[1])
    return start, end


async def _build_report(db: AsyncSession, month: str) -> list[MonthlyReportRow]:
    start, end = _month_bounds(month)

    employees_result = await db.execute(select(User).where(User.role == "employee").order_by(User.name))
    employees = employees_result.scalars().all()

    days_result = await db.execute(
        select(AttendanceDay).where(AttendanceDay.date >= start, AttendanceDay.date <= end)
    )
    days = days_result.scalars().all()

    days_by_user: dict[int, list[AttendanceDay]] = {}
    for d in days:
        days_by_user.setdefault(d.user_id, []).append(d)

    rows = []
    for emp in employees:
        emp_days = days_by_user.get(emp.id, [])

        def count(status_value: str, days: list[AttendanceDay] = emp_days) -> int:
            return sum(1 for d in days if d.status == status_value)

        rows.append(
            MonthlyReportRow(
                user_id=emp.id,
                name=emp.name,
                email=emp.email,
                present_days=count("present"),
                late_days=count("late"),
                flagged_days=count("flagged"),
                absent_days=count("absent"),
                leave_days=count("leave"),
                off_days=count("off_day"),
                gps_days=sum(1 for d in emp_days if d.method == "gps"),
                gps_pending_days=sum(1 for d in emp_days if d.verification == "gps_pending"),
            )
        )
    return rows


@router.get("", response_model=list[MonthlyReportRow])
async def monthly_report(
    month: str = Query(..., description="YYYY-MM"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    return await _build_report(db, month)


@router.get("/export")
async def export_report(
    month: str = Query(..., description="YYYY-MM"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    rows = await _build_report(db, month)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Name", "Email", "Present", "Late", "Flagged", "Absent", "Leave", "Off Days", "GPS Days", "GPS Pending"]
    )
    for r in rows:
        writer.writerow(
            [r.name, r.email, r.present_days, r.late_days, r.flagged_days, r.absent_days,
             r.leave_days, r.off_days, r.gps_days, r.gps_pending_days]
        )
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=attendance-report-{month}.csv"},
    )
