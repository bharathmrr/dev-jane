"""Pytest configuration: set minimal env so settings import, force LLM stub."""
import os

os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)
os.environ.setdefault("EMAIL_HMAC_SECRET", "y" * 16)
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("LLM_PROVIDER", "stub")
