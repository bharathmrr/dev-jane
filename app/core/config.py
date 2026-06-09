"""Centralized, environment-driven configuration.

All settings are read from environment variables (or a local .env in dev).
Nothing here should hardcode a secret; secrets come from the environment
or a secrets manager in production.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- App ---
    ENV: Literal["dev", "staging", "prod"] = "dev"
    DEBUG: bool = False
    APP_NAME: str = "jane-aerospace-scheduler"
    API_V1_PREFIX: str = "/api/v1"
    DEFAULT_TIMEZONE: str = "UTC"

    # --- Security (optional for LinkedIn-only mode) ---
    JWT_SECRET_KEY: str = Field("dev-jwt-secret-key-not-for-production-use-change-me", min_length=32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = 30
    REFRESH_TOKEN_TTL_DAYS: int = 14
    EMAIL_HMAC_SECRET: str = Field("dev-hmac-secret-change-me", min_length=16)

    # --- Database (optional; not required for LinkedIn outreach mode) ---
    DATABASE_URL: Optional[PostgresDsn] = None
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30

    # --- Redis (optional; not required for LinkedIn outreach mode) ---
    REDIS_URL: Optional[RedisDsn] = None
    RESERVATION_TTL_SECONDS: int = 600
    LOCK_TIMEOUT_SECONDS: int = 5

    # --- Celery ---
    CELERY_BROKER_URL: Optional[RedisDsn] = None
    CELERY_RESULT_BACKEND: Optional[RedisDsn] = None
    IMAP_POLL_INTERVAL_SECONDS: int = 60
    REMINDER_LEAD_MINUTES: int = 60

    # --- SMTP (outbound) ---
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_FROM_EMAIL: str = "scheduler@example.com"
    SMTP_FROM_NAME: str = "Scheduling Assistant"
    INBOUND_DOMAIN: str = "reply.example.com"

    # --- IMAP (inbound) ---
    IMAP_HOST: str = "localhost"
    IMAP_PORT: int = 993
    IMAP_USERNAME: str = ""
    IMAP_PASSWORD: str = ""
    IMAP_MAILBOX: str = "INBOX"
    IMAP_USE_SSL: bool = True

    # --- LLM ---
    LLM_PROVIDER: Literal["anthropic", "openai", "stub"] = "stub"
    ANTHROPIC_API_KEY: str = ""
    LLM_INTENT_MODEL: str = "claude-haiku-4-5"
    LLM_NEGOTIATION_MODEL: str = "claude-sonnet-4-6"
    LLM_MAX_TOKENS: int = 1024
    LLM_TIMEOUT_SECONDS: int = 20

    # --- Rate limiting ---
    RATE_LIMIT_PER_MINUTE: int = 120

    # --- Observability ---
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False
    PROMETHEUS_ENABLED: bool = False
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None

    # --- SendGrid ---
    SENDGRID_API_KEY: str = ""
    SENDGRID_FROM_EMAIL: str = "bharath.p@janeaerospace.co.in"

    # --- Calendly & App URL ---
    CALENDLY_LINK: str = "https://calendly.com/bharathreddyget"
    CALENDLY_API_TOKEN: str = ""
    APP_URL: str = "http://localhost:8000"

    # --- Google Sheets ---
    GOOGLE_SHEETS_CREDENTIALS_JSON: str = ""
    GOOGLE_SHEETS_SPREADSHEET_ID: str = ""
    GOOGLE_SHEETS_WORKSHEET_NAME: str = "tested-csv"

    # --- Zoho Bookings ---
    ZOHO_CLIENT_ID: str = ""
    ZOHO_CLIENT_SECRET: str = ""
    ZOHO_REFRESH_TOKEN: str = ""
    ZOHO_SERVICE_ID: str = ""
    ZOHO_STAFF_ID: str = ""
    ZOHO_DC: str = "com"

    # --- Groq (email generation) ---
    GROQ_API_KEY: str = ""

    # --- Organizer contact ---
    ORGANIZER_EMAIL: str = "bharath.p@janeaerospace.co.in"
    ORGANIZER_NAME: str = "Leo Charles"

    # --- Zoho availability cache ---
    ZOHO_CACHE_TTL: int = 300

    # --- Zoho WorkDrive (file uploads for onboarding docs) ---
    ZOHO_WORKDRIVE_TEAM_ID: str = ""
    ZOHO_WORKDRIVE_FOLDER_ID: str = ""          # root folder for onboarding uploads
    ZOHO_NDA_TEMPLATE_ID_INDIAN: str = ""       # Zoho WorkDrive file ID for Indian NDA template
    ZOHO_NDA_TEMPLATE_ID_OVERSEAS: str = ""
    ZOHO_AGREEMENT_TEMPLATE_ID_INDIAN: str = ""
    ZOHO_AGREEMENT_TEMPLATE_ID_OVERSEAS: str = ""

    # --- Onboarding ---
    ONBOARDING_HMAC_SECRET: str = Field("dev-onboarding-hmac-change-me", min_length=16)
    ONBOARDING_WORKSHEET_NAME: str = "Onboarding"
    LEADS_CSV_PATH: str = "/app/leads.csv"

    # --- Zoho CRM (contact sync at every onboarding stage) ---
    ZOHO_CRM_REFRESH_TOKEN: str = ""

    # --- Third-party KYC Verification ---
    KYC_PROVIDER: str = "free"           # "free" (current) | "setu" | "karza" | "cashfree"
    SETU_CLIENT_ID: str = ""
    SETU_CLIENT_SECRET: str = ""
    SETU_BASE_URL: str = "https://dg-sandbox.setu.co"   # sandbox; prod: https://dg.setu.co
    KARZA_API_KEY: str = ""
    KARZA_BASE_URL: str = "https://testapi.karza.in"    # sandbox; prod: https://api.karza.in

    # --- Zoho Contracts (e-sign for NDA & Customer Agreement) ---
    ZOHO_CONTRACTS_CLIENT_ID: str = ""       # Self Client ID (has ZohoWriter scope)
    ZOHO_CONTRACTS_CLIENT_SECRET: str = ""   # Self Client Secret
    ZOHO_CONTRACTS_REFRESH_TOKEN: str = ""
    ZOHO_CONTRACTS_ORG_ID: str = ""
    ZOHO_CONTRACTS_NDA_TEMPLATE_INDIAN: str = ""
    ZOHO_CONTRACTS_NDA_TEMPLATE_OVERSEAS: str = ""
    ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_INDIAN: str = ""
    ZOHO_CONTRACTS_AGREEMENT_TEMPLATE_OVERSEAS: str = ""

    # --- Zoho Sign (e-signature — requires paid-plan credentials for sending) ---
    ZOHO_SIGN_CLIENT_ID: str = ""
    ZOHO_SIGN_CLIENT_SECRET: str = ""
    ZOHO_SIGN_REFRESH_TOKEN: str = ""
    ZOHO_SIGN_NDA_TEMPLATE_ID: str = ""
    ZOHO_SIGN_NDA_TEMPLATE_ACTION_JANE: str = ""
    ZOHO_SIGN_NDA_TEMPLATE_ACTION_COUNTERPARTY: str = ""

    # --- Onboarding reviewer (receives KYC/NDA/Agreement preview emails) ---
    ONBOARDING_REVIEWER_EMAIL: str = ""


    @field_validator("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", mode="before")
    @classmethod
    def _default_celery_to_redis(cls, v, info):
        return v or info.data.get("REDIS_URL")

    @property
    def database_url_sync(self) -> str:
        """Sync DSN (psycopg) for Alembic and Celery workers."""
        if not self.DATABASE_URL:
            return ""
        url = str(self.DATABASE_URL).replace("postgresql+asyncpg", "postgresql+psycopg")
        return url.replace("?ssl=require", "?sslmode=require").replace("&ssl=require", "&sslmode=require")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
