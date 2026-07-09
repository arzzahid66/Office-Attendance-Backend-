from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.audit import log_action
from app.config import get_settings
from app.database import get_db
from app.deps import require_admin
from app.ip_utils import get_client_ip, is_non_public_ip, normalize_ip
from app.models import Office, Roster, User
from app.schemas import OfficeCreate, OfficeOut, OfficeUpdate, PublicIpIn

router = APIRouter()
settings = get_settings()


def _validate_ip(ip: str) -> str:
    canonical = normalize_ip(ip)
    if canonical is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"'{ip}' is not a valid IP address")
    if not settings.dev_mode and is_non_public_ip(canonical):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Only globally-routable public IPs can be office IPs. Private, loopback, "
            "link-local, CGNAT (100.64.0.0/10) and documentation ranges are rejected. "
            "Set DEV_MODE=true for local testing.",
        )
    return canonical


def _validate_timezone(tz: str) -> str:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"'{tz}' is not a valid IANA timezone")
    return tz


async def _get_office(db: AsyncSession, office_id: int) -> Office:
    office = await db.get(Office, office_id)
    if office is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Office not found")
    return office


async def _add_ip(db: AsyncSession, office: Office, ip: str, admin_id: int) -> None:
    canonical = _validate_ip(ip)
    if canonical in office.public_ips:
        return
    office.public_ips = [*office.public_ips, canonical]
    flag_modified(office, "public_ips")  # JSONB in-place mutation isn't auto-tracked
    await log_action(db, admin_id, "office_ip_added", {"office_id": office.id, "ip": canonical})


@router.get("", response_model=list[OfficeOut])
async def list_offices(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    result = await db.execute(select(Office).order_by(Office.created_at.desc()))
    return result.scalars().all()


@router.post("", response_model=OfficeOut, status_code=status.HTTP_201_CREATED)
async def create_office(payload: OfficeCreate, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    _validate_timezone(payload.timezone)
    canonical_ips: list[str] = []
    for ip in payload.public_ips:
        c = _validate_ip(ip)
        if c not in canonical_ips:
            canonical_ips.append(c)

    office = Office(
        name=payload.name,
        public_ips=canonical_ips,
        latitude=payload.latitude,
        longitude=payload.longitude,
        radius_meters=payload.radius_meters,
        timezone=payload.timezone,
        active=payload.active,
    )
    db.add(office)
    await db.flush()
    await log_action(db, admin.id, "office_created", {"office_id": office.id, "name": office.name})
    await db.commit()
    await db.refresh(office)
    return office


@router.get("/current-ip")
async def current_ip(request: Request, _: User = Depends(require_admin)):
    """Shows the admin's detected public IP without saving it — so they can confirm the
    office WiFi's egress IP before adding it. Requires TRUST_PROXY=true behind a proxy."""
    return {"detected_ip": get_client_ip(request)}


@router.patch("/{office_id}", response_model=OfficeOut)
async def update_office(
    office_id: int, payload: OfficeUpdate, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    office = await _get_office(db, office_id)
    data = payload.model_dump(exclude_unset=True)
    if "timezone" in data:
        _validate_timezone(data["timezone"])
    for field, value in data.items():
        setattr(office, field, value)
    await log_action(db, admin.id, "office_updated", {"office_id": office.id, "fields": list(data.keys())})
    await db.commit()
    await db.refresh(office)
    return office


@router.post("/{office_id}/public-ips", response_model=OfficeOut)
async def add_public_ip(
    office_id: int, payload: PublicIpIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    office = await _get_office(db, office_id)
    await _add_ip(db, office, payload.ip, admin.id)
    await db.commit()
    await db.refresh(office)
    return office


@router.post("/{office_id}/add-current-ip", response_model=OfficeOut)
async def add_current_ip(
    office_id: int, request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    office = await _get_office(db, office_id)
    await _add_ip(db, office, get_client_ip(request), admin.id)
    await db.commit()
    await db.refresh(office)
    return office


@router.delete("/{office_id}/public-ips", response_model=OfficeOut)
async def remove_public_ip(
    office_id: int, ip: str, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    office = await _get_office(db, office_id)
    canonical = normalize_ip(ip) or ip
    if canonical not in office.public_ips:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"'{ip}' is not one of this office's IPs")
    office.public_ips = [x for x in office.public_ips if x != canonical]
    flag_modified(office, "public_ips")
    await log_action(db, admin.id, "office_ip_removed", {"office_id": office.id, "ip": canonical})
    await db.commit()
    await db.refresh(office)
    return office


@router.delete("/{office_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_office(office_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)):
    office = await _get_office(db, office_id)
    # 409 if any roster references this office — the FK is NOT NULL, so a raw delete 500s.
    count = (
        await db.execute(select(func.count(Roster.id)).where(Roster.office_id == office_id))
    ).scalar_one()
    if count:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot delete: {count} roster(s) belong to this office. Move or delete them first.",
        )
    await log_action(db, admin.id, "office_deleted", {"office_id": office_id, "name": office.name})
    await db.delete(office)
    await db.commit()
