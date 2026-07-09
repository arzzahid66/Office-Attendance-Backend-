from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Office(Base):
    """A physical branch. `public_ips` is the set of egress IPs seen when on its WiFi;
    lat/long + radius_meters back the GPS fallback. Coordinates are nullable so the
    office can be seeded as a placeholder and filled in from the admin UI."""

    __tablename__ = "offices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    public_ips: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    radius_meters: Mapped[int] = mapped_column(Integer, nullable=False, default=80, server_default="80")
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Karachi", server_default="Asia/Karachi"
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Roster(Base):
    """Shift timing AND working days together — some staff work 10-19 Mon-Fri, others
    10-19 Fri-Sun. `working_days` uses Python's weekday() encoding: 0=Mon .. 6=Sun.
    Overnight shifts are out of scope for Phase 1 (see ck_rosters_time_order)."""

    __tablename__ = "rosters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    grace_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15, server_default="15")
    working_days: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    office_id: Mapped[int] = mapped_column(ForeignKey("offices.id"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    office: Mapped["Office"] = relationship(lazy="selectin")

    __table_args__ = (CheckConstraint("end_time > start_time", name="ck_rosters_time_order"),)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="employee")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    job_title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Both are kept: we must know what the employee asked for vs what admin granted.
    requested_roster_id: Mapped[int | None] = mapped_column(ForeignKey("rosters.id"), nullable=True)
    assigned_roster_id: Mapped[int | None] = mapped_column(ForeignKey("rosters.id"), nullable=True)
    admin_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    devices: Mapped[list["Device"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    # selectin (not lazy="select") because async SQLAlchemy raises MissingGreenlet on implicit lazy loads.
    assigned_roster: Mapped["Roster | None"] = relationship(foreign_keys=[assigned_roster_id], lazy="selectin")
    requested_roster: Mapped["Roster | None"] = relationship(foreign_keys=[requested_roster_id], lazy="selectin")

    __table_args__ = (
        CheckConstraint("role in ('admin','employee')", name="ck_users_role"),
        CheckConstraint("status in ('pending','active','rejected','disabled')", name="ck_users_status"),
    )


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="devices")
    heartbeats: Mapped[list["Heartbeat"]] = relationship(back_populates="device", cascade="all, delete-orphan")

    __table_args__ = (CheckConstraint("status in ('active','revoked')", name="ck_devices_status"),)


class Heartbeat(Base):
    __tablename__ = "heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_matched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    device: Mapped["Device"] = relationship(back_populates="heartbeats")


class AttendanceDay(Base):
    """One row per (user, office-local date). `date` is ALWAYS the office-local date,
    never the UTC date. Written only via presence.apply_presence_event()."""

    __tablename__ = "attendance_days"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    verification: Mapped[str | None] = mapped_column(String(20), nullable=True)
    check_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    check_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    late_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_attendance_days_user_date"),
        CheckConstraint(
            "status in ('present','late','flagged','absent','leave','off_day')",
            name="ck_attendance_days_status",
        ),
        CheckConstraint("method in ('wifi','gps','manual')", name="ck_attendance_days_method"),
        CheckConstraint("verification in ('verified','gps_pending')", name="ck_attendance_days_verification"),
    )


class CheckinAttempt(Base):
    """Append-only audit of EVERY check-in attempt, successful or not. A double-tap
    correctly produces two rows. This is also where GPS re-verification results land."""

    __tablename__ = "checkin_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    matched_office_id: Mapped[int | None] = mapped_column(
        ForeignKey("offices.id", ondelete="SET NULL"), nullable=True
    )
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_meters: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("method in ('wifi','gps')", name="ck_checkin_attempts_method"),
        CheckConstraint(
            "result in ('success','ip_no_match','outside_radius','low_accuracy',"
            "'before_shift','after_shift','off_day','already_checked_in')",
            name="ck_checkin_attempts_result",
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppState(Base):
    """Cross-process key/value state. Holds the DEV_MODE `time_override` so that
    /debug/set-time reaches the separate scheduler process (a module global would not)."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
