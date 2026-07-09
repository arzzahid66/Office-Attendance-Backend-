from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log_action
from app.database import get_db
from app.deps import get_current_user
from app.device_service import find_active_device, register_device
from app.models import Roster, User
from app.schemas import (
    AccessTokenResponse,
    LoginRequest,
    LoginResponse,
    MeOut,
    RefreshRequest,
    SignupRequest,
    UserOut,
)
from app.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password

router = APIRouter()

# Constant-time-ish guard against user enumeration: we always run one bcrypt verify,
# even when the email doesn't exist, so response timing doesn't reveal account existence.
_DUMMY_HASH = "$2b$12$" + "x" * 53


async def _resolve_requested_roster(db: AsyncSession, roster_id: int | None) -> Roster:
    """A requested roster must reference an ACTIVE roster. When omitted we fall back to
    the default roster — requested_roster_id is never stored as NULL."""
    if roster_id is not None:
        roster = await db.get(Roster, roster_id)
        if roster is None or not roster.active:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Requested roster does not exist or is inactive")
        return roster

    result = await db.execute(select(Roster).where(Roster.is_default.is_(True), Roster.active.is_(True)).limit(1))
    default = result.scalar_one_or_none()
    if default is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "No default roster is configured. Ask an admin to set one before signing up.",
        )
    return default


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)):
    email = payload.email.lower()
    roster = await _resolve_requested_roster(db, payload.requested_roster_id)

    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

    if existing is not None:
        # Re-signup is allowed ONLY for a rejected account. Permitting it for an active
        # account would let anyone overwrite an employee's password via the public
        # signup endpoint; permitting it for a disabled one would bypass the disable.
        if existing.status == "pending":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your signup is already pending admin approval")
        if existing.status != "rejected":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email already registered")

        existing.name = payload.name
        existing.phone = payload.phone
        existing.password_hash = hash_password(payload.password)
        existing.department = payload.department
        existing.job_title = payload.job_title
        existing.city = payload.city
        existing.requested_roster_id = roster.id
        existing.assigned_roster_id = None
        existing.status = "pending"
        existing.admin_feedback = None  # a fresh application starts without the old rejection note
        existing.approved_by = None
        existing.approved_at = None
        # The previous rejection stays in audit_logs — the trail is never rewritten.
        await log_action(db, existing.id, "signup_resubmitted", {"email": email, "requested_roster_id": roster.id})
        await db.commit()
        return {"message": "Signup resubmitted. Your account is pending admin approval."}

    user = User(
        name=payload.name,
        email=email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role="employee",
        status="pending",
        department=payload.department,
        job_title=payload.job_title,
        city=payload.city,
        requested_roster_id=roster.id,
        assigned_roster_id=None,
    )
    db.add(user)
    await db.flush()
    await log_action(db, user.id, "signup", {"email": email, "requested_roster_id": roster.id})
    await db.commit()
    return {"message": "Signup successful. Your account is pending admin approval."}


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
):
    email = payload.email.lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

    # Bad credentials stay a generic 401 and never reveal whether the email exists.
    if user is None:
        verify_password(payload.password, _DUMMY_HASH)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    # Credentials are correct — now it is safe to explain the account state.
    if user.status == "pending":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            user.admin_feedback or "Your account is pending admin approval.",
        )
    if user.status == "rejected":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            user.admin_feedback or "Your signup was rejected. Contact your admin.",
        )
    if user.status == "disabled":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account has been disabled.")

    device_token_out = None
    if user.role == "employee":
        existing_device = None
        if x_device_token:
            existing_device = await find_active_device(db, user, x_device_token)
        if existing_device is None:
            device_token_out = await register_device(db, user, request.headers.get("user-agent"))

    access_token = create_access_token(user.id, user.role)
    refresh_token = create_refresh_token(user.id)
    await db.commit()

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        device_token=device_token_out,
        user=UserOut.model_validate(user),
    )


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        data = decode_token(payload.refresh_token)
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")
    if data.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token type")

    user = await db.get(User, int(data["sub"]))
    if user is None or user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer active")

    return AccessTokenResponse(access_token=create_access_token(user.id, user.role))


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)):
    # assigned_roster is eager-loaded (lazy="selectin"), so the client learns its shift window.
    return MeOut.model_validate(user)
