"""Centralized, environment-driven configuration.

All settings are read from environment variables (or a local .env in dev).
Nothing here should hardcode a secret; secrets come from the environment
or a secrets manager in production.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- App ---
    ENV: Literal["dev", "staging", "prod"] = "dev"
    DEBUG: bool = False
    APP_NAME: str = "email-scheduler"
    API_V1_PREFIX: str = "/api/v1"
    DEFAULT_TIMEZONE: str = "UTC"

    # --- Security ---
    JWT_SECRET_KEY: str = Field(..., min_length=32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = 30
    REFRESH_TOKEN_TTL_DAYS: int = 14
    # HMAC secret for verifying inbound email webhooks / reply tokens.
    EMAIL_HMAC_SECRET: str = Field(..., min_length=16)

    # --- Database ---
    DATABASE_URL: PostgresDsn
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30

    # --- Redis ---
    REDIS_URL: RedisDsn
    RESERVATION_TTL_SECONDS: int = 600  # 10 minute slot hold (BookMyShow-style)
    LOCK_TIMEOUT_SECONDS: int = 5  # how long to wait to acquire a lock

    # --- Celery ---
    CELERY_BROKER_URL: RedisDsn | None = None
    CELERY_RESULT_BACKEND: RedisDsn | None = None
    IMAP_POLL_INTERVAL_SECONDS: int = 60
    REMINDER_LEAD_MINUTES: int = 60  # send reminder N minutes before meeting

    # --- SMTP (outbound) ---
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_FROM_EMAIL: str = "scheduler@example.com"
    SMTP_FROM_NAME: str = "Scheduling Assistant"
    # Address replies are routed to; the local-part may carry a thread token.
    INBOUND_DOMAIN: str = "reply.example.com"

    # --- IMAP (inbound) ---
    IMAP_HOST: str = "localhost"
    IMAP_PORT: int = 993
    IMAP_USERNAME: str = ""
    IMAP_PASSWORD: str = ""
    IMAP_MAILBOX: str = "INBOX"
    IMAP_USE_SSL: bool = True

    # --- LLM ---
    LLM_PROVIDER: Literal["anthropic", "openai", "stub"] = "anthropic"
    ANTHROPIC_API_KEY: str = ""
    # Cheap/fast model for intent classification; larger model for negotiation.
    LLM_INTENT_MODEL: str = "claude-haiku-4-5"
    LLM_NEGOTIATION_MODEL: str = "claude-sonnet-4-6"
    LLM_MAX_TOKENS: int = 1024
    LLM_TIMEOUT_SECONDS: int = 20

    # --- Rate limiting ---
    RATE_LIMIT_PER_MINUTE: int = 120

    # --- Observability ---
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True
    PROMETHEUS_ENABLED: bool = True
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None

    @field_validator("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", mode="before")
    @classmethod
    def _default_celery_to_redis(cls, v, info):
        # If Celery URLs are not set, fall back to REDIS_URL.
        return v or info.data.get("REDIS_URL")

    @property
    def database_url_sync(self) -> str:
        """Sync DSN (psycopg) for Alembic and Celery workers."""
        url = str(self.DATABASE_URL).replace("postgresql+asyncpg", "postgresql+psycopg")
        # asyncpg uses ?ssl=require; psycopg uses ?sslmode=require
        return url.replace("?ssl=require", "?sslmode=require").replace("&ssl=require", "&sslmode=require")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
