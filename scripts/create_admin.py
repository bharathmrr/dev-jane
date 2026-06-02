"""One-time script to create the super-admin user."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import select
from app.db.session import SessionLocal as AsyncSessionLocal
from app.db.models import User
from app.db.base import UserRole
from app.core.security import hash_password
import uuid


async def main(email: str, password: str, name: str = "Super Admin"):
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing:
            if existing.role != UserRole.ADMIN:
                existing.role = UserRole.ADMIN
                await db.commit()
                print(f"Promoted existing user {email} to admin.")
            else:
                print(f"Admin user {email} already exists.")
            return

        user = User(
            id=uuid.uuid4(),
            email=email,
            full_name=name,
            hashed_password=hash_password(password),
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(user)
        await db.commit()
        print(f"Created admin: {email}")


if __name__ == "__main__":
    email = sys.argv[1] if len(sys.argv) > 1 else "bharathreddyget@gmail.com"
    password = sys.argv[2] if len(sys.argv) > 2 else "admin123"
    name = sys.argv[3] if len(sys.argv) > 3 else "Super Admin"
    asyncio.run(main(email, password, name))
