# Deployment Guide

## 1. Prerequisites

- Docker + Docker Compose (single host) **or** Kubernetes (scale-out).
- A reachable PostgreSQL 16 and Redis 7 (managed services recommended in prod).
- An SMTP relay for outbound, and either IMAP credentials or an inbound-email
  webhook provider (SendGrid/Mailgun/SES) pointed at `/webhooks/email/inbound`.
- An LLM API key (Anthropic) — or run `LLM_PROVIDER=stub` for a key-free demo.

## 2. Configuration

All config is environment-driven (`app/core/config.py`). Copy `.env.example`
and set, at minimum:

```bash
JWT_SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
EMAIL_HMAC_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST:5432/scheduler
REDIS_URL=redis://HOST:6379/0
ENV=prod
LOG_JSON=true
```

In production, inject secrets from your secrets manager (AWS Secrets Manager,
Vault, Kubernetes Secrets) rather than a committed `.env`. Never commit `.env`.

## 3. Database migrations

```bash
alembic upgrade head        # baseline 0001 creates the full schema
# future changes:
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

The Compose `api` service runs `alembic upgrade head` on start. In Kubernetes,
run it as a one-shot **init job** before rolling out the API, not in every pod.

## 4. Seed an admin + organizer

There is no public sign-up (account creation is intentionally restricted). Seed
the first admin via a short script / psql, then use the API:

```python
# scripts/seed.py (sketch)
import asyncio
from app.db.session import SessionLocal
from app.db.models import User
from app.db.base import UserRole
from app.core.security import hash_password

async def main():
    async with SessionLocal() as db:
        db.add(User(email="admin@you.com", full_name="Admin",
                    hashed_password=hash_password("change-me"),
                    role=UserRole.ADMIN))
        await db.commit()

asyncio.run(main())
```

Then `POST /api/v1/admin/organizers` with weekly availability rules.

## 5. Running the services

**Single host (Compose):**

```bash
docker compose up -d --build                 # api, worker, beat, pg, redis, nginx
docker compose --profile monitoring up -d    # + prometheus, grafana
```

Processes you must run in any environment:
- **API**: `gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w <cpu*2+1>`
- **Worker**: `celery -A app.workers.celery_app:celery_app worker -Q email,reminders,maintenance`
- **Beat (exactly one)**: `celery -A app.workers.celery_app:celery_app beat`

Run a single beat instance to avoid duplicate periodic jobs. Scale API and
worker replicas horizontally; consider separate worker deployments per queue.

## 6. Nginx / TLS

`nginx/nginx.conf` reverse-proxies to the API, applies edge rate limiting,
adds security headers, and blocks `/metrics` publicly. For TLS on a single host,
terminate with the commented `443` block + Let's Encrypt, or put a managed load
balancer in front and keep Nginx HTTP-only behind it.

## 7. Observability

- Scrape `/metrics` from inside the network (Prometheus config provided).
- Grafana is provisioned with a Prometheus datasource; import or build
  dashboards for request latency/error rate, Celery queue depth, reservation
  conflict rate, and email send/parse outcomes.
- Logs are JSON with `request_id`; ship to your log stack (Loki/ELK/CloudWatch).
- Add OTLP tracing by setting `OTEL_EXPORTER_OTLP_ENDPOINT` and wiring an
  OpenTelemetry instrumentor (hook point left in config).

## 8. Health & rollout

- Liveness: `GET /api/v1/health/live`
- Readiness: `GET /api/v1/health/ready` (checks Postgres + Redis) — gate
  traffic on this in your orchestrator.

## 9. Scaling notes (toward 100k leads / 10k bookings-day)

- Postgres: connection pooling (PgBouncer), a read replica for `/analytics`.
- Redis: managed cluster; upgrade the lock to multi-node Redlock for HA.
- Workers: autoscale the `email` queue on backlog; keep `maintenance` small.
- Email: use a reputable relay with SPF/DKIM/DMARC to protect deliverability;
  prefer the inbound **webhook** over IMAP polling at high volume.

## 10. Security checklist before go-live

- [ ] Strong, rotated `JWT_SECRET_KEY` and `EMAIL_HMAC_SECRET` from a vault
- [ ] `ENV=prod` (disables `/docs`), restrictive CORS origins
- [ ] TLS everywhere; `/metrics` not publicly exposed
- [ ] DB user least-privilege; backups + PITR enabled
- [ ] SPF/DKIM/DMARC configured for the sending domain
- [ ] Rate limits tuned; WAF in front if internet-facing
- [ ] Audit log retention/forwarding configured
