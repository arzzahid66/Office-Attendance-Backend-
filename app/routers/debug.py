from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import require_dev_mode
from app.ip_utils import get_client_ip
from app.models import RandomCheck, User
from app.schemas import RandomCheckOut
from app.timeutil import now_utc, today_local

router = APIRouter(dependencies=[Depends(require_dev_mode)])


@router.get("/my-ip")
async def my_ip(request: Request):
    return {"detected_ip": get_client_ip(request)}


@router.post("/trigger-check/{user_id}", response_model=RandomCheckOut, status_code=status.HTTP_201_CREATED)
async def trigger_check(user_id: int, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if user is None or user.role != "employee":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")

    check = RandomCheck(user_id=user_id, date=today_local(), scheduled_at=now_utc(), result="pending")
    db.add(check)
    await db.commit()
    await db.refresh(check)
    return check
