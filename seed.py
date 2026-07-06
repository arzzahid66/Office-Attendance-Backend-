"""Seeds two sample active employees and today's random checks for an instant demo.

Run from the project root: python -m backend.seed
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select  # noqa: E402

from app.checks_service import generate_checks_for_day  # noqa: E402
from app.database import AsyncSessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.timeutil import today_local  # noqa: E402

SEED_PASSWORD = "test1234"
SEED_EMPLOYEES = [
    {"name": "Aisha Khan", "email": "aisha@example.com"},
    {"name": "Bilal Ahmed", "email": "bilal@example.com"},
]


async def main() -> None:
    async with AsyncSessionLocal() as db:
        for emp in SEED_EMPLOYEES:
            result = await db.execute(select(User).where(User.email == emp["email"]))
            user = result.scalar_one_or_none()
            if user is None:
                db.add(
                    User(
                        name=emp["name"],
                        email=emp["email"],
                        password_hash=hash_password(SEED_PASSWORD),
                        role="employee",
                        status="active",
                    )
                )
                print(f"Created employee {emp['email']} (password: {SEED_PASSWORD})")
            else:
                user.status = "active"
                print(f"Employee {emp['email']} already exists, ensured active")
        await db.commit()

    async with AsyncSessionLocal() as db:
        created = await generate_checks_for_day(db, today_local())
        print(f"Generated {created} random check(s) for today ({today_local()})")

    print("\nSeed complete. Sign in as an employee with the emails above and password 'test1234'.")
    print("Don't forget to add an office network - DEV_MODE allows 127.0.0.1 for local testing:")
    print("  Admin > Networks > 'Add my current IP as office network'")


if __name__ == "__main__":
    asyncio.run(main())
