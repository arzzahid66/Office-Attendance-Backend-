from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log_action
from app.database import get_db
from app.deps import require_active_employee, require_admin
from app.models import User, WfhRequest
from app.schemas import WfhDecisionIn, WfhRequestIn, WfhRequestOut
from app.timeutil import now_local, now_utc, today_local

router = APIRouter()


@router.post("/wfh", response_model=WfhRequestOut, status_code=status.HTTP_201_CREATED)
async def create_wfh_request(
    payload: WfhRequestIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_active_employee)
):
    today_date = today_local()
    if payload.date < today_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot request WFH for a past date")
    if payload.date == today_date and now_local().hour >= 9:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Same-day WFH must be requested before 09:00")

    request_row = WfhRequest(user_id=user.id, date=payload.date, reason=payload.reason, status="pending")
    db.add(request_row)
    await log_action(db, user.id, "wfh_requested", f"date={payload.date}")
    await db.commit()
    await db.refresh(request_row)
    return request_row


@router.get("/wfh/mine", response_model=list[WfhRequestOut])
async def my_wfh_requests(db: AsyncSession = Depends(get_db), user: User = Depends(require_active_employee)):
    result = await db.execute(select(WfhRequest).where(WfhRequest.user_id == user.id).order_by(WfhRequest.date.desc()))
    return result.scalars().all()


@router.get("/admin/wfh", response_model=list[WfhRequestOut])
async def list_wfh_requests(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    result = await db.execute(select(WfhRequest).order_by(WfhRequest.date.desc()))
    return result.scalars().all()


@router.post("/admin/wfh/{request_id}/decide", response_model=WfhRequestOut)
async def decide_wfh_request(
    request_id: int, payload: WfhDecisionIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    request_row = await db.get(WfhRequest, request_id)
    if request_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "WFH request not found")
    request_row.status = payload.status
    request_row.decided_by = admin.id
    request_row.decided_at = now_utc()
    await log_action(db, admin.id, "wfh_decided", f"request_id={request_id} status={payload.status}")
    await db.commit()
    await db.refresh(request_row)
    return request_row
