"""Seeds the admin, office, rosters and demo employees. Safe to run repeatedly (idempotent).

The admin is created by reusing app.bootstrap.ensure_admin_user (the same upsert the app
runs at startup), so ADMIN_EMAIL / ADMIN_PASSWORD stay the single source of truth.

Rev A of the migration already inserts 'Head Office' and the three rosters; this script
re-asserts them so a hand-modified database can be brought back to a known state.

Run from the project root: python -m backend.seed
"""

import asyncio
import sys
from datetime import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select  # noqa: E402

from app.bootstrap import ensure_admin_user  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models import Office, Roster, User  # noqa: E402
from app.security import hash_password  # noqa: E402

SEED_PASSWORD = "test1234"

OFFICE_NAME = "Head Office"

# All Mon-Fri. 0=Mon .. 6=Sun (matches Python's date.weekday()).
ROSTERS = [
    {"name": "Morning", "start_time": time(10, 0), "end_time": time(19, 0), "is_default": True},
    {"name": "Mid", "start_time": time(11, 0), "end_time": time(20, 0), "is_default": False},
    {"name": "Late", "start_time": time(12, 0), "end_time": time(21, 0), "is_default": False},
]

# 1 pending (awaiting approval, has only a *requested* roster) + 2 active on different rosters.
EMPLOYEES = [
    {
        "name": "Aisha Khan",
        "email": "aisha@example.com",
        "phone": "+92 300 1111111",
        "department": "Engineering",
        "job_title": "Senior Developer",
        "city": "Lahore",
        "status": "active",
        "roster": "Morning",
    },
    {
        "name": "Bilal Ahmed",
        "email": "bilal@example.com",
        "phone": "+92 300 2222222",
        "department": "Support",
        "job_title": "Support Lead",
        "city": "Karachi",
        "status": "active",
        "roster": "Late",
    },
    {
        "name": "Sana Malik",
        "email": "sana@example.com",
        "phone": "+92 300 3333333",
        "department": "Design",
        "job_title": "Product Designer",
        "city": "Islamabad",
        "status": "pending",
        "roster": "Mid",
    },
]


async def ensure_office(db) -> Office:
    office = (await db.execute(select(Office).where(Office.name == OFFICE_NAME))).scalar_one_or_none()
    if office is None:
        # Placeholder coordinates on purpose — set the real lat/long from Admin > Offices.
        office = Office(
            name=OFFICE_NAME,
            public_ips=[],
            latitude=None,
            longitude=None,
            radius_meters=80,
            timezone="Asia/Karachi",
            active=True,
        )
        db.add(office)
        await db.flush()
        print(f"Created office {OFFICE_NAME!r} (lat/long are placeholders - set them in Admin > Offices)")
    else:
        print(f"Office {OFFICE_NAME!r} already exists")
    return office


async def ensure_rosters(db, office: Office) -> dict[str, Roster]:
    by_name: dict[str, Roster] = {}
    for spec in ROSTERS:
        roster = (await db.execute(select(Roster).where(Roster.name == spec["name"]))).scalar_one_or_none()
        if roster is None:
            roster = Roster(
                name=spec["name"],
                start_time=spec["start_time"],
                end_time=spec["end_time"],
                grace_minutes=15,
                working_days=[0, 1, 2, 3, 4],
                is_default=spec["is_default"],
                office_id=office.id,
                active=True,
            )
            db.add(roster)
            await db.flush()
            print(
                f"Created roster {spec['name']!r} "
                f"{spec['start_time']:%H:%M}-{spec['end_time']:%H:%M} (Mon-Fri)"
                + (" [default]" if spec["is_default"] else "")
            )
        else:
            print(f"Roster {spec['name']!r} already exists")
        by_name[spec["name"]] = roster
    return by_name


async def ensure_employees(db, rosters: dict[str, Roster]) -> None:
    for spec in EMPLOYEES:
        user = (await db.execute(select(User).where(User.email == spec["email"]))).scalar_one_or_none()
        roster = rosters[spec["roster"]]
        is_active = spec["status"] == "active"

        if user is None:
            db.add(
                User(
                    name=spec["name"],
                    email=spec["email"],
                    phone=spec["phone"],
                    password_hash=hash_password(SEED_PASSWORD),
                    role="employee",
                    status=spec["status"],
                    department=spec["department"],
                    job_title=spec["job_title"],
                    city=spec["city"],
                    requested_roster_id=roster.id,
                    # A pending user has requested a roster but has not been granted one yet.
                    assigned_roster_id=roster.id if is_active else None,
                )
            )
            print(f"Created {spec['status']} employee {spec['email']} on roster {spec['roster']!r}")
        else:
            user.status = spec["status"]
            user.requested_roster_id = roster.id
            user.assigned_roster_id = roster.id if is_active else None
            print(f"Employee {spec['email']} already exists, re-asserted {spec['status']}")


async def main() -> None:
    async with AsyncSessionLocal() as db:
        # Same upsert the app runs at startup; ADMIN_EMAIL/ADMIN_PASSWORD remain the source of truth.
        await ensure_admin_user(db)
        print(f"Ensured admin {get_settings().admin_email.lower()}")

    async with AsyncSessionLocal() as db:
        office = await ensure_office(db)
        rosters = await ensure_rosters(db, office)
        await ensure_employees(db, rosters)
        await db.commit()

    print(f"\nSeed complete. Employee password: {SEED_PASSWORD!r}")
    print("Next steps:")
    print("  1. Admin > Offices > set the real latitude/longitude (GPS fallback needs them)")
    print("  2. Admin > Offices > 'Add my current public IP' (DEV_MODE allows private IPs)")
    print("  3. Admin > Approvals > approve sana@example.com to test the approval flow")


if __name__ == "__main__":
    asyncio.run(main())
