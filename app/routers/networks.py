from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log_action
from app.config import get_settings
from app.database import get_db
from app.deps import require_admin
from app.ip_utils import get_client_ip, is_local_ip
from app.models import OfficeNetwork, User
from app.schemas import OfficeNetworkIn, OfficeNetworkOut, OfficeNetworkUpdate

router = APIRouter()
settings = get_settings()


def _validate_ip_allowed(ip: str) -> None:
    if not settings.dev_mode and is_local_ip(ip):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Local/private IPs can only be added as office networks when DEV_MODE=true",
        )


@router.get("", response_model=list[OfficeNetworkOut])
async def list_networks(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    result = await db.execute(select(OfficeNetwork).order_by(OfficeNetwork.created_at.desc()))
    return result.scalars().all()


@router.post("", response_model=OfficeNetworkOut, status_code=status.HTTP_201_CREATED)
async def create_network(
    payload: OfficeNetworkIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    _validate_ip_allowed(payload.public_ip)
    network = OfficeNetwork(
        label=payload.label, public_ip=payload.public_ip, active=payload.active, created_by=admin.id
    )
    db.add(network)
    await log_action(db, admin.id, "office_network_created", f"{payload.label} ({payload.public_ip})")
    await db.commit()
    await db.refresh(network)
    return network


@router.post("/add-current-ip", response_model=OfficeNetworkOut, status_code=status.HTTP_201_CREATED)
async def add_current_ip(
    request: Request, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    ip = get_client_ip(request)
    _validate_ip_allowed(ip)
    network = OfficeNetwork(label=f"Auto-added ({ip})", public_ip=ip, active=True, created_by=admin.id)
    db.add(network)
    await log_action(db, admin.id, "office_network_created", f"auto-added current ip {ip}")
    await db.commit()
    await db.refresh(network)
    return network


@router.patch("/{network_id}", response_model=OfficeNetworkOut)
async def update_network(
    network_id: int,
    payload: OfficeNetworkUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    network = await db.get(OfficeNetwork, network_id)
    if network is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Office network not found")
    if payload.public_ip is not None:
        _validate_ip_allowed(payload.public_ip)
        network.public_ip = payload.public_ip
    if payload.label is not None:
        network.label = payload.label
    if payload.active is not None:
        network.active = payload.active
    await log_action(db, admin.id, "office_network_updated", f"id={network_id}")
    await db.commit()
    await db.refresh(network)
    return network


@router.delete("/{network_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_network(
    network_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)
):
    network = await db.get(OfficeNetwork, network_id)
    if network is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Office network not found")
    await db.delete(network)
    await log_action(db, admin.id, "office_network_deleted", f"id={network_id}")
    await db.commit()
