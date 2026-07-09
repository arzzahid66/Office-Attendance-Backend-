from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log_action
from app.database import get_db
from app.deps import require_admin
from app.models import Office, Roster, User
from app.schemas import RosterCreate, RosterOut, RosterUpdate

router = APIRouter()


async def _get_roster(db: AsyncSession, roster_id: int) -> Roster:
    roster = await db.get(Roster, roster_id)
    if roster is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Roster not found")
    return roster


async def _clear_other_defaults(db: AsyncSession, keep_id: int | None) -> None:
    q = select(Roster).where(Roster.is_default.is_(True))
    if keep_id is not None:
        q = q.where(Roster.id != keep_id)
    for other in (await db.execute(q)).scalars().all():
        other.is_default = False


# --------------------------------------------------------------------------- public
@router.get("", response_model=list[RosterOut])
async def list_active_rosters(db: AsyncSession = Depends(get_db)):
    """Public on purpose: the signup form must render the roster radio buttons before
    any account (and therefore any token) exists. Only non-sensitive shift metadata."""
    result = await db.execute(select(Roster).where(Roster.active.is_(True)).order_by(Roster.start_time.asc()))
    return result.scalars().all()


# --------------------------------------------------------------------------- admin
@router.get("/all", response_model=list[RosterOut])
async def list_all_rosters(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    """Admin view: includes inactive rosters (the public endpoint hides those)."""
    result = await db.execute(select(Roster).order_by(Roster.start_time.asc()))
    return result.scalars().all()


@router.post("", response_model=RosterOut, status_code=status.HTTP_201_CREATED)
async def create_roster(payload: RosterCreate, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    if payload.end_time <= payload.start_time:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "end_time must be after start_time")
    if await db.get(Office, payload.office_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "office_id does not reference an existing office")

    roster = Roster(
        name=payload.name,
        start_time=payload.start_time,
        end_time=payload.end_time,
        grace_minutes=payload.grace_minutes,
        working_days=payload.working_days,
        is_default=payload.is_default,
        office_id=payload.office_id,
        active=payload.active,
    )
    db.add(roster)
    await db.flush()
    if payload.is_default:
        await _clear_other_defaults(db, keep_id=roster.id)
    await log_action(db, admin.id, "roster_created", {"roster_id": roster.id, "name": roster.name})
    await db.commit()
    await db.refresh(roster)
    return roster


@router.patch("/{roster_id}", response_model=RosterOut)
async def update_roster(
    roster_id: int, payload: RosterUpdate, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    roster = await _get_roster(db, roster_id)
    data = payload.model_dump(exclude_unset=True)

    # Validate the MERGED values, not just the ones supplied.
    new_start = data.get("start_time", roster.start_time)
    new_end = data.get("end_time", roster.end_time)
    if new_end <= new_start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "end_time must be after start_time")
    if "office_id" in data and await db.get(Office, data["office_id"]) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "office_id does not reference an existing office")

    for field, value in data.items():
        setattr(roster, field, value)
    if data.get("is_default") is True:
        await _clear_other_defaults(db, keep_id=roster.id)

    await log_action(db, admin.id, "roster_updated", {"roster_id": roster.id, "fields": list(data.keys())})
    await db.commit()
    await db.refresh(roster)
    return roster


@router.delete("/{roster_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_roster(roster_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    roster = await _get_roster(db, roster_id)

    # 409 if any user still references this roster (assigned OR requested) — otherwise the
    # FK blocks the delete with a raw 500, and an assigned employee would be orphaned.
    in_use = await db.execute(
        select(func.count(User.id)).where(
            or_(User.assigned_roster_id == roster_id, User.requested_roster_id == roster_id)
        )
    )
    count = in_use.scalar_one()
    if count:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot delete: {count} user(s) reference this roster. Reassign them first "
            "(or deactivate the roster instead).",
        )

    await log_action(db, admin.id, "roster_deleted", {"roster_id": roster_id, "name": roster.name})
    await db.delete(roster)
    await db.commit()
