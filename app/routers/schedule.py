from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log_action
from app.database import get_db
from app.deps import require_active_employee, require_admin
from app.models import ScheduleRequest, User
from app.schemas import (
    ScheduleDecisionIn,
    ScheduleRequestAdminOut,
    ScheduleRequestIn,
    ScheduleRequestOut,
)
from app.timeutil import now_utc, today_local

router = APIRouter()


@router.post("/schedule", response_model=ScheduleRequestOut, status_code=status.HTTP_201_CREATED)
async def create_schedule_request(
    payload: ScheduleRequestIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_active_employee)
):
    if payload.end_date < payload.start_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "End date must be on or after start date")
    if payload.start_date < today_local():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot request a schedule for a past start date")
    if payload.end_time <= payload.start_time:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "End time must be after start time")

    request_row = ScheduleRequest(
        user_id=user.id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        start_time=payload.start_time,
        end_time=payload.end_time,
        reason=payload.reason,
        status="pending",
    )
    db.add(request_row)
    await log_action(
        db, user.id, "schedule_requested", f"{payload.start_date}..{payload.end_date} {payload.start_time}-{payload.end_time}"
    )
    await db.commit()
    await db.refresh(request_row)
    return request_row


@router.get("/schedule/mine", response_model=list[ScheduleRequestOut])
async def my_schedule_requests(db: AsyncSession = Depends(get_db), user: User = Depends(require_active_employee)):
    result = await db.execute(
        select(ScheduleRequest)
        .where(ScheduleRequest.user_id == user.id)
        .order_by(ScheduleRequest.start_date.desc())
    )
    return result.scalars().all()


@router.get("/admin/schedule", response_model=list[ScheduleRequestAdminOut])
async def list_schedule_requests(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    result = await db.execute(
        select(ScheduleRequest, User)
        .join(User, User.id == ScheduleRequest.user_id)
        .order_by(ScheduleRequest.start_date.desc())
    )
    return [
        ScheduleRequestAdminOut(
            id=req.id,
            user_id=req.user_id,
            start_date=req.start_date,
            end_date=req.end_date,
            start_time=req.start_time,
            end_time=req.end_time,
            reason=req.reason,
            status=req.status,
            decided_by=req.decided_by,
            decided_at=req.decided_at,
            decision_note=req.decision_note,
            name=user.name,
            email=user.email,
            department=user.department,
            job_title=user.job_title,
            city=user.city,
        )
        for req, user in result.all()
    ]


@router.post("/admin/schedule/{request_id}/decide", response_model=ScheduleRequestOut)
async def decide_schedule_request(
    request_id: int, payload: ScheduleDecisionIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    request_row = await db.get(ScheduleRequest, request_id)
    if request_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule request not found")
    if request_row.status != "pending":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This request has already been decided")

    request_row.status = payload.status
    request_row.decided_by = admin.id
    request_row.decided_at = now_utc()
    request_row.decision_note = payload.note
    await log_action(db, admin.id, "schedule_decided", f"request_id={request_id} status={payload.status}")
    await db.commit()
    await db.refresh(request_row)
    return request_row
