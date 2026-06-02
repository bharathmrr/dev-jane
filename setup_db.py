"""
Run once to create all tables and a default admin user.

Usage:
    python setup_db.py

Default credentials after running:
    email   : admin@example.com
    password: Admin1234!
"""
import asyncio
import sys

from sqlalchemy import text


async def main():
    # Import here so .env is loaded first
    from app.core.config import settings
    from app.db.session import engine, SessionLocal
    from app.db.base import Base
    from app.db.models import User  # noqa: F401 — registers all models
    import app.db.models  # noqa: F401 — ensure every model is registered

    print(f"[SETUP] Connecting to: {str(settings.DATABASE_URL)[:60]}...")

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[SETUP] All tables created (or already existed).")

    # Create default admin user
    from app.core.security import hash_password
    from app.db.base import UserRole
    from sqlalchemy import select

    async with SessionLocal() as db:
        existing = (
            await db.execute(select(User).where(User.email == "admin@example.com"))
        ).scalar_one_or_none()

        if existing:
            print("[SETUP] Admin user already exists — skipping.")
        else:
            admin = User(
                email="admin@example.com",
                full_name="Admin User",
                hashed_password=hash_password("Admin1234!"),
                role=UserRole.ADMIN,
                is_active=True,
            )
            db.add(admin)
            await db.commit()
            print("[SETUP] Admin user created:")
            print("        email   : admin@example.com")
            print("        password: Admin1234!")

    await engine.dispose()
    print("[SETUP] Done.")


if __name__ == "__main__":
    asyncio.run(main())
