from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    platform: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: Literal["admin", "employee"]
    status: Literal["pending", "active", "disabled"]
    created_at: datetime

    model_config = {"from_attributes": True}


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    device_token: str | None = None
    user: UserOut


class AccessTokenResponse(BaseModel):
    access_token: str


class DeviceOut(BaseModel):
    id: int
    platform: str | None
    status: Literal["active", "revoked"]
    created_at: datetime
    last_seen_at: datetime | None

    model_config = {"from_attributes": True}


class OfficeNetworkIn(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    public_ip: str = Field(min_length=1, max_length=64)
    active: bool = True


class OfficeNetworkUpdate(BaseModel):
    label: str | None = None
    public_ip: str | None = None
    active: bool | None = None


class OfficeNetworkOut(BaseModel):
    id: int
    label: str
    public_ip: str
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class RandomCheckOut(BaseModel):
    id: int
    date: date
    scheduled_at: datetime
    responded_at: datetime | None
    source_ip: str | None
    result: Literal["pending", "passed", "missed"]

    model_config = {"from_attributes": True}


class AttendanceDayOut(BaseModel):
    id: int
    date: date
    mode: Literal["office", "wfh", "flagged", "absent", "pending"]
    check_in: datetime | None
    check_out: datetime | None
    passed_checks: int
    total_checks: int
    note: str | None

    model_config = {"from_attributes": True}


class TodayOut(BaseModel):
    detected_ip: str
    ip_matched: bool
    attendance: AttendanceDayOut | None
    checks: list[RandomCheckOut]


class WfhRequestIn(BaseModel):
    date: date
    reason: str = Field(min_length=1, max_length=1000)


class WfhDecisionIn(BaseModel):
    status: Literal["approved", "rejected"]


class WfhRequestOut(BaseModel):
    id: int
    user_id: int
    date: date
    reason: str
    status: Literal["pending", "approved", "rejected"]
    decided_by: int | None
    decided_at: datetime | None

    model_config = {"from_attributes": True}


class FlaggedResolveIn(BaseModel):
    mode: Literal["office", "wfh", "flagged", "absent"]
    note: str | None = None


class EmployeeOut(BaseModel):
    id: int
    name: str
    email: str
    status: Literal["pending", "active", "disabled"]
    created_at: datetime
    active_device_count: int


class DashboardEntryOut(BaseModel):
    user_id: int
    name: str
    email: str
    mode: Literal["office", "wfh", "flagged", "absent", "pending"]
    check_in: datetime | None
    check_out: datetime | None
    last_heartbeat_at: datetime | None


class FlaggedDayOut(BaseModel):
    id: int
    user_id: int
    name: str
    email: str
    date: date
    mode: Literal["office", "wfh", "flagged", "absent", "pending"]
    check_in: datetime | None
    check_out: datetime | None
    passed_checks: int
    total_checks: int
    note: str | None


class MonthlyReportRow(BaseModel):
    user_id: int
    name: str
    email: str
    office_days: int
    wfh_days: int
    flagged_days: int
    absent_days: int
    pending_days: int
