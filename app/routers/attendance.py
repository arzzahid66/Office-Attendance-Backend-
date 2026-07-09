"""Core check-in endpoints. The SERVER decides everything (off-day / before-shift /
after-shift / late / wifi vs gps) so rules can change without shipping a new client.

  POST /checkin-attempt   -> WiFi path; falls back to `need_location` when the IP misses
  POST /verify-location   -> GPS path; re-runs the shift guards (never trust client timing)
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.deps import get_current_device, require_active_employee
from app.ip_utils import get_client_ip
from app.models import AttendanceDay, CheckinAttempt, Device, Heartbeat, User
from app.presence import (
    PresenceEvent,
    apply_presence_event,
    ensure_off_day,
    evaluate_shift,
    find_office_by_ip,
    nearest_office,
)
from app.schemas import AttendanceDayOut, HeartbeatIn, LocationIn
from app.timeutil import now_utc, office_today, today_local
from datetime import timedelta
from sqlalchemy import select

router = APIRouter()
settings = get_settings()


def _fmt_remaining(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} hour{'s' if h != 1 else ''} {m} minute{'s' if m != 1 else ''}"
    if h:
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{m} minute{'s' if m != 1 else ''}"


async def _log(db, user_id, method, result, *, source_ip=None, office_id=None, lat=None, lng=None, accuracy=None, distance=None, message=None):
    db.add(
        CheckinAttempt(
            user_id=user_id,
            method=method,
            result=result,
            source_ip=source_ip,
            matched_office_id=office_id,
            latitude=lat,
            longitude=lng,
            gps_accuracy=accuracy,
            distance_meters=distance,
            message=message,
        )
    )


def _checked_in_payload(day, method: str) -> dict:
    return {
        "state": "checked_in",
        "method": method,
        "status": day.status,
        "check_in": day.check_in,
        "late_minutes": day.late_minutes,
        "verification": day.verification,
        "message": "Check-in done ✅"
        + (" (location verified)" if method == "gps" else "")
        + (f" — late by {day.late_minutes} min" if day.status == "late" else ""),
    }


async def _guard_shift(db: AsyncSession, user: User, method: str):
    """Shared guards for both endpoints. Returns (early_response | None, ev, roster, office).
    `now` is resolved once and reused so both endpoints see identical timing."""
    roster = user.assigned_roster
    if roster is None:
        # 200 with a state (not an HTTP error): the client renders a friendly card, and
        # this is a business outcome, not a failure. Still distinct from every check-in state.
        return ({"state": "no_roster", "message": "Roster not assigned, contact admin."}, None, None, None)

    office = roster.office
    now = now_utc()
    ev = evaluate_shift(office, roster, now)

    if not ev.is_working_day:
        await ensure_off_day(db, user.id, ev.local_date)
        await _log(db, user.id, method, "off_day", message="off_day")
        await db.commit()
        return ({"state": "off_day", "message": "Aaj aap ki chhutti hai."}, None, None, None)

    if ev.phase == "before_shift":
        await _log(db, user.id, method, "before_shift", message="before_shift")
        await db.commit()
        return (
            {
                "state": "before_shift",
                "message": f"Office time starts at {ev.shift_start:%H:%M} — "
                f"{_fmt_remaining(ev.minutes_to_start)} remaining.",
                "starts_at": ev.shift_start,
                "minutes_remaining": ev.minutes_to_start,
            },
            None,
            None,
            None,
        )

    if ev.phase == "after_shift":
        await _log(db, user.id, method, "after_shift", message="after_shift")
        await db.commit()
        return (
            {"state": "after_shift", "message": "Shift is over. Contact your manager for manual attendance."},
            None,
            None,
            None,
        )

    return (None, ev, roster, office)


@router.post("/checkin-attempt")
async def checkin_attempt(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_active_employee),
    _device: Device = Depends(get_current_device),
):
    early, ev, roster, office = await _guard_shift(db, user, "wifi")
    if early is not None:
        return early

    ip = get_client_ip(request)
    matched = await find_office_by_ip(db, ip)

    if matched is None:
        await _log(db, user.id, "wifi", "ip_no_match", source_ip=ip, message="ip_no_match")
        await db.commit()
        return {
            "state": "need_location",
            "message": "Aap office network pe detect nahi hue. Check-in ke liye office WiFi se "
            "connect karein, ya location share karke verify karein.",
        }

    event = PresenceEvent(user_id=user.id, timestamp=now_utc(), method="wifi",
                          source_ip=ip, office_id=matched.id, confidence=1.0)
    day, checked_in = await apply_presence_event(db, event, ev, roster)
    if not checked_in:
        await db.commit()
        return {"state": "already_checked_in", "status": day.status, "check_in": day.check_in, "method": day.method}

    await _log(db, user.id, "wifi", "success", source_ip=ip, office_id=matched.id, message="wifi check-in")
    await db.commit()
    return _checked_in_payload(day, "wifi")


@router.post("/verify-location")
async def verify_location(
    payload: LocationIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_active_employee),
    _device: Device = Depends(get_current_device),
):
    # Re-run the shift guards: the user may have sat on the popup for an hour.
    early, ev, roster, office = await _guard_shift(db, user, "gps")
    if early is not None:
        return early

    lat, lng, acc = payload.latitude, payload.longitude, payload.accuracy

    if acc > settings.max_gps_accuracy_m:
        await _log(db, user.id, "gps", "low_accuracy", lat=lat, lng=lng, accuracy=acc, message="low_accuracy")
        await db.commit()
        return {
            "state": "low_accuracy",
            "message": "Location signal weak. Move near a window and retry.",
            "accuracy": acc,
        }

    found = await nearest_office(db, lat, lng)
    if found is None:
        await _log(db, user.id, "gps", "outside_radius", lat=lat, lng=lng, accuracy=acc, message="no office coords")
        await db.commit()
        return {"state": "outside_radius", "message": "Office location is not configured yet. Contact admin."}

    office_hit, distance = found
    if distance > office_hit.radius_meters:
        await _log(db, user.id, "gps", "outside_radius", lat=lat, lng=lng, accuracy=acc,
                   distance=distance, office_id=office_hit.id, message="outside_radius")
        await db.commit()
        return {
            "state": "outside_radius",
            "message": f"Aap office se {round(distance)} meter door hain. Check-in nahi ho saka.",
            "distance_meters": round(distance),
        }

    event = PresenceEvent(user_id=user.id, timestamp=now_utc(), method="gps", lat=lat, lng=lng,
                          office_id=office_hit.id, confidence=0.6)
    day, checked_in = await apply_presence_event(db, event, ev, roster)
    if not checked_in:
        await db.commit()
        return {"state": "already_checked_in", "status": day.status, "check_in": day.check_in, "method": day.method}

    await _log(db, user.id, "gps", "success", lat=lat, lng=lng, accuracy=acc, distance=distance,
               office_id=office_hit.id, message="gps check-in")
    await db.commit()
    return _checked_in_payload(day, "gps")


@router.get("/history", response_model=list[AttendanceDayOut])
async def history(db: AsyncSession = Depends(get_db), user: User = Depends(require_active_employee)):
    roster = user.assigned_roster
    local_today = office_today(roster.office) if roster else today_local()
    cutoff = local_today - timedelta(days=30)
    result = await db.execute(
        select(AttendanceDay)
        .where(AttendanceDay.user_id == user.id, AttendanceDay.date >= cutoff)
        .order_by(AttendanceDay.date.desc())
    )
    return result.scalars().all()


@router.post("/heartbeat")
async def heartbeat(
    payload: HeartbeatIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_active_employee),
    device: Device = Depends(get_current_device),
):
    """Presence maintenance while the app is open. Advances attendance_days.last_seen only
    for time we can verify: an IP match, or (on a GPS day) a fresh in-radius location fix.
    Runs only inside the shift window. See the GPS check-out rule in the plan.
    """
    roster = user.assigned_roster
    if roster is None:
        return {"state": "no_roster", "in_shift": False}

    office = roster.office
    now = now_utc()
    ev = evaluate_shift(office, roster, now)
    base = {
        "shift_start": ev.shift_start,
        "shift_end": ev.shift_end,
        "reverify_minutes": settings.gps_reverify_minutes,
    }

    if ev.phase != "in_shift":
        # Outside the window (off_day / before_shift / after_shift): record nothing.
        return {"state": ev.phase, "in_shift": False, **base}

    ip = get_client_ip(request)
    matched_office = await find_office_by_ip(db, ip)
    ip_matched = matched_office is not None
    db.add(Heartbeat(user_id=user.id, device_id=device.id, source_ip=ip, ip_matched=ip_matched))
    device.last_seen_at = now

    day = (
        await db.execute(select(AttendanceDay).where(AttendanceDay.user_id == user.id, AttendanceDay.date == ev.local_date))
    ).scalar_one_or_none()

    if day is None or day.check_in is None:
        await db.commit()
        return {"state": "not_checked_in", "in_shift": True, "requires_location": False, **base}

    requires_location = False

    if ip_matched:
        # On the office network: always the strongest signal.
        day.last_seen = now
        if day.method == "gps":
            # Mid-day upgrade: they joined office WiFi, so the day becomes verified.
            day.method = "wifi"
            day.verification = "verified"
            await _log(db, user.id, "wifi", "success", source_ip=ip, office_id=matched_office.id,
                       message="gps->wifi upgrade")
    elif day.method == "gps":
        # Off the office network on a GPS day: only a fresh in-radius fix counts.
        if payload.latitude is not None and payload.longitude is not None and payload.accuracy is not None:
            if payload.accuracy > settings.max_gps_accuracy_m:
                await _log(db, user.id, "gps", "low_accuracy", lat=payload.latitude, lng=payload.longitude,
                           accuracy=payload.accuracy, message="reverify low_accuracy")
                requires_location = True
            else:
                found = await nearest_office(db, payload.latitude, payload.longitude)
                if found and found[1] <= found[0].radius_meters:
                    day.last_seen = now
                    await _log(db, user.id, "gps", "success", lat=payload.latitude, lng=payload.longitude,
                               accuracy=payload.accuracy, distance=found[1], office_id=found[0].id,
                               message="gps reverify ok")
                else:
                    dist = round(found[1]) if found else None
                    await _log(db, user.id, "gps", "outside_radius", lat=payload.latitude, lng=payload.longitude,
                               accuracy=payload.accuracy, distance=found[1] if found else None,
                               office_id=found[0].id if found else None, message="reverify outside_radius")
                    requires_location = True  # last_seen frozen -> idle job will close the day
        else:
            requires_location = True
    # else: wifi day but IP no longer matches -> freeze last_seen (they left the office).

    await db.commit()
    return {
        "state": "ok",
        "in_shift": True,
        "requires_location": requires_location,
        "method": day.method,
        "last_seen": day.last_seen,
        **base,
    }
