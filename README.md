# Email-Driven Meeting Scheduler

A SaaS-grade, **email-only** meeting scheduling engine modeled on BookMyShow's
seat-reservation architecture. Leads receive available slots by email and book
by replying in natural language ("YES 2", "Monday 11", "can we do Wed 5pm?").
There are no calendar links, no booking pages — the email reply *is* the UI.

The hard problems it solves: preventing double-booking under concurrency
(distributed locking + a database-level guard), understanding free-text replies
(LLM intent classification), timezone/DST-correct slot generation, and an
automated negotiation loop when a requested time is gone.

> **Status / scope.** The application core (config, security, models, the full
> domain layer, API, Celery workers, migration, tests) is implemented and
> import-verified; the pure-logic test suite passes. It is a strong, runnable
> foundation rather than a turnkey enterprise product — see
> [`ARCHITECTURE.md`](ARCHITECTURE.md) for what's complete vs. where to extend
> (e.g. exact free-text date parsing, multi-node Redlock, Grafana dashboards).

## Stack

FastAPI · SQLAlchemy 2.0 (async) · PostgreSQL · Alembic · Redis · Celery ·
SMTP/IMAP · JWT · Docker Compose · Nginx · Prometheus/Grafana · structlog.
LLM intent parsing is provider-agnostic (Anthropic by default, with a
deterministic stub so CI needs no API key).

## Quickstart (local, with a fake mail server)

```bash
cp .env.example .env
# set JWT_SECRET_KEY and EMAIL_HMAC_SECRET to long random values; optionally
# set ANTHROPIC_API_KEY, or leave LLM_PROVIDER=stub to run without a key.

# Bring up API + worker + beat + postgres + redis + mailpit (fake SMTP/IMAP UI)
docker compose --profile dev up --build
```

- API docs: <http://localhost/docs> (proxied via Nginx) or `:8000/docs` direct
- Fake mailbox UI (Mailpit): <http://localhost:8025>
- Metrics (blocked at the edge, internal only): `api:8000/metrics`

Migrations run automatically on API start (`alembic upgrade head`).

### Try the flow

1. `POST /api/v1/auth/login` to get a JWT (seed an admin user first — see
   `scripts/seed.py` placeholder in DEPLOYMENT).
2. `POST /api/v1/admin/organizers` with working-hours rules.
3. `POST /api/v1/leads` with a name + email + organizer_id. This fires an offer
   email (visible in Mailpit).
4. Reply to that email (in Mailpit) with e.g. `YES 2`. The IMAP poller (or the
   `/webhooks/email/inbound` endpoint) ingests it, the LLM classifies intent,
   the slot is reserved (Redis lock + DB hold) and confirmed, and a confirmation
   email goes out.

## End-to-end flow

```
Lead created ──▶ Offer email (5 slots) ──▶ Lead replies (free text)
        │                                          │
        │                                   LLM intent classify
        │                                          │
        ▼                  confirm ──▶ reserve (Redis lock + DB hold, 10 min)
   state machine                              │
   (audited)                    won ──▶ confirm ──▶ confirmation email
        ▲                        │
        │                       lost ──▶ nearest alternatives email
        └────────── reschedule / cancel / re-offer ◀── reject / suggest
```

## Tests

```bash
pip install -r requirements.txt
LLM_PROVIDER=stub pytest -q        # slot generation, state machine, intent
```

CI (`.github/workflows/ci.yml`) additionally spins up Postgres + Redis, runs
migrations, lint (ruff), and builds the Docker image.

## Layout

```
app/
  core/      config, security (JWT + HMAC), redis lock, logging, deps
  db/        base/enums, ORM models, async session
  services/  slot_generator, availability, reservation, conflict_resolver,
             email_intent (+ llm_client), booking, email_processor, notifications/
  api/v1/    auth, bookings/leads, system (webhook/admin/analytics/health)
  workers/   celery_app, email/reminder/reservation tasks, runtime bridge
  state_machine.py
migrations/  alembic env + 0001 baseline
tests/       pure-logic + stub-intent suites
nginx/ monitoring/ .github/workflows/
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`DEPLOYMENT.md`](DEPLOYMENT.md).
