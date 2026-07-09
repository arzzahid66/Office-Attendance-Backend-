from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

# NOTE: attendance / dashboard / report schemas are reintroduced in Steps 4 and 7,
# once checkin-attempt and the admin screens are rebuilt on the new model.


def _validate_working_days(days: list[int]) -> list[int]:
    if not days:
        raise ValueError("working_days must contain at least one day")
    if any(d < 0 or d > 6 for d in days):
        raise ValueError("working_days must be ints 0..6 (0=Mon .. 6=Sun)")
    if len(set(days)) != len(days):
        raise ValueError("working_days must not contain duplicates")
    return sorted(set(days))


# --------------------------------------------------------------------------- rosters
class RosterOut(BaseModel):
    id: int
    name: str
    start_time: time
    end_time: time
    grace_minutes: int
    working_days: list[int]  # 0=Mon .. 6=Sun
    is_default: bool
    office_id: int
    active: bool

    model_config = {"from_attributes": True}


class RosterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    start_time: time
    end_time: time
    grace_minutes: int = Field(default=15, ge=0, le=240)
    working_days: list[int]
    is_default: bool = False
    office_id: int
    active: bool = True

    _wd = field_validator("working_days")(_validate_working_days)


class RosterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    start_time: time | None = None
    end_time: time | None = None
    grace_minutes: int | None = Field(default=None, ge=0, le=240)
    working_days: list[int] | None = None
    is_default: bool | None = None
    office_id: int | None = None
    active: bool | None = None

    @field_validator("working_days")
    @classmethod
    def _wd(cls, v: list[int] | None) -> list[int] | None:
        return None if v is None else _validate_working_days(v)


# --------------------------------------------------------------------------- offices
class OfficeOut(BaseModel):
    id: int
    name: str
    public_ips: list[str]
    latitude: float | None
    longitude: float | None
    radius_meters: int
    timezone: str
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class OfficeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    public_ips: list[str] = Field(default_factory=list)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_meters: int = Field(default=80, ge=10, le=100000)
    timezone: str = Field(default="Asia/Karachi", max_length=64)
    active: bool = True


class OfficeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_meters: int | None = Field(default=None, ge=10, le=100000)
    timezone: str | None = Field(default=None, max_length=64)
    active: bool | None = None


class PublicIpIn(BaseModel):
    ip: str = Field(min_length=3, max_length=64)


# --------------------------------------------------------------------------- attendance
class LocationIn(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float = Field(ge=0)  # metres, from browser coords.accuracy


class HeartbeatIn(BaseModel):
    # Optional location: on a GPS day the client re-sends coords every GPS_REVERIFY_MINUTES
    # so the server can keep advancing last_seen only while the user stays in radius.
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)


class SetTimeIn(BaseModel):
    # Aware ISO-8601 UTC (e.g. "2026-07-14T21:30:00+00:00"), or null to clear.
    iso: str | None = None


class AttendanceDayOut(BaseModel):
    id: int
    date: date
    status: Literal["present", "late", "flagged", "absent", "leave", "off_day"]
    method: Literal["wifi", "gps", "manual"] | None
    verification: Literal["verified", "gps_pending"] | None
    check_in: datetime | None
    check_out: datetime | None
    late_minutes: int

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- auth
class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    phone: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=8, max_length=128)
    # Optional: when omitted we fall back to the default roster (never stored as NULL).
    requested_roster_id: int | None = None
    department: str = Field(min_length=1, max_length=120)
    job_title: str = Field(min_length=1, max_length=120)
    city: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    phone: str | None
    role: Literal["admin", "employee"]
    status: Literal["pending", "active", "rejected", "disabled"]
    department: str | None
    job_title: str | None
    city: str | None
    requested_roster_id: int | None
    assigned_roster_id: int | None
    admin_feedback: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class MeOut(UserOut):
    """/auth/me — includes the resolved roster so the client knows its shift window."""

    assigned_roster: RosterOut | None = None


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    device_token: str | None = None
    user: UserOut


class AccessTokenResponse(BaseModel):
    access_token: str


# --------------------------------------------------------------------------- devices
class DeviceOut(BaseModel):
    id: int
    user_agent: str | None
    status: Literal["active", "revoked"]
    created_at: datetime
    last_seen_at: datetime | None

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- admin
class UserDecisionIn(BaseModel):
    action: Literal["approve", "reject"]
    # Required when action='approve'; validated against an ACTIVE roster in the router.
    assigned_roster_id: int | None = None
    feedback: str | None = Field(default=None, max_length=1000)


class EmployeeOut(BaseModel):
    id: int
    name: str
    email: str
    phone: str | None
    status: Literal["pending", "active", "rejected", "disabled"]
    department: str | None
    job_title: str | None
    city: str | None
    requested_roster_id: int | None
    assigned_roster_id: int | None
    admin_feedback: str | None
    created_at: datetime
    active_device_count: int


AttendanceStatus = Literal["present", "late", "flagged", "absent", "leave", "off_day"]


class DashboardEntryOut(BaseModel):
    """Today's live view, one row per active employee (day may not exist yet)."""

    user_id: int
    name: str
    email: str
    roster_name: str | None
    shift_start: time | None
    shift_end: time | None
    day_id: int | None
    status: AttendanceStatus | None
    method: Literal["wifi", "gps", "manual"] | None
    verification: Literal["verified", "gps_pending"] | None
    check_in: datetime | None
    check_out: datetime | None
    last_seen: datetime | None
    late_minutes: int


class FlaggedDayOut(BaseModel):
    id: int
    user_id: int
    name: str
    email: str
    date: date
    status: AttendanceStatus
    method: Literal["wifi", "gps", "manual"] | None
    verification: Literal["verified", "gps_pending"] | None
    check_in: datetime | None
    check_out: datetime | None
    late_minutes: int
    admin_note: str | None


class FlaggedResolveIn(BaseModel):
    # A human decides the final state. The nightly job NEVER writes 'absent'.
    status: Literal["present", "absent", "leave"]
    note: str | None = Field(default=None, max_length=1000)


class VerifyDecisionIn(BaseModel):
    # approve -> trust the GPS check-in; query -> send it to flagged for follow-up.
    action: Literal["approve", "query"]
    note: str | None = Field(default=None, max_length=1000)


class MonthlyReportRow(BaseModel):
    user_id: int
    name: str
    email: str
    present_days: int
    late_days: int
    flagged_days: int
    absent_days: int
    leave_days: int
    off_days: int
    gps_days: int
    gps_pending_days: int
