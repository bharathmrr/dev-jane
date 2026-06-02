"""
Standalone LinkedIn Outreach Automation — FastAPI entry point.

Run with:
    uvicorn linkedin_main:app --reload --port 8000

This deliberately does NOT import app.api.v1.router (the full scheduler stack)
so it starts without PostgreSQL, Redis, or Celery being configured.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.config import settings
    print(f"[STARTUP] LinkedIn Outreach Bot starting — ENV={settings.ENV}")
    print(f"[STARTUP] Unipile DSN : {settings.UNIPILE_DSN}")
    print(f"[STARTUP] SendGrid From: {settings.SENDGRID_FROM_EMAIL}")
    print(f"[STARTUP] Calendly    : {settings.CALENDLY_LINK}")
    print(f"[STARTUP] App URL     : {settings.APP_URL}")
    print(f"[STARTUP] Form URL    : {settings.APP_URL}/form")
    print(f"[STARTUP] Webhook URL : {settings.APP_URL}/webhook/linkedin  <-- set this in Unipile")
    yield
    print("[SHUTDOWN] LinkedIn Outreach Bot stopped.")


app = FastAPI(
    title="LinkedIn Outreach Bot",
    version="1.0.0",
    description="Automates LinkedIn lead outreach via Unipile + SendGrid",
    lifespan=lifespan,
)

app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount only the LinkedIn outreach router — no DB/Redis needed
from app.api.v1.linkedin import router as linkedin_router
app.include_router(linkedin_router)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    print(f"[ERROR] Unhandled exception on {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health():
    return {"status": "ok", "service": "linkedin-outreach"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("linkedin_main:app", host="0.0.0.0", port=8000, reload=True)
