from fastapi import APIRouter, Depends, Header, HTTPException, status
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.device_service import find_active_device, register_device
from app.models import User
from app.schemas import AccessTokenResponse, LoginRequest, LoginResponse, RefreshRequest, SignupRequest, UserOut
from app.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password

router = APIRouter()


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupRequest, db: AsyncSession = Depends(get_db)):
    email = payload.email.lower()
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email already registered")

    user = User(
        name=payload.name,
        email=email,
        password_hash=hash_password(payload.password),
        role="employee",
        status="pending",
    )
    db.add(user)
    await db.commit()
    return {"message": "Signup successful. Your account is pending admin approval."}


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
    x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
):
    email = payload.email.lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if user.status == "pending":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account is pending admin approval")
    if user.status == "disabled":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account has been disabled")

    device_token_out = None
    if user.role == "employee":
        existing_device = None
        if x_device_token:
            existing_device = await find_active_device(db, user, x_device_token)
        if existing_device is None:
            device_token_out = await register_device(db, user, payload.platform)

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


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)
