from fastapi import APIRouter

from app.api.v1 import (
    auth,
    bookings,
    dashboard_endpoints,
    document_endpoints,
    logs_endpoints,
    onboarding_endpoints,
    system,
    v2_endpoints,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(system.router)   # /bookings/list must come before /bookings/{id}
api_router.include_router(bookings.router)
api_router.include_router(v2_endpoints.router)
api_router.include_router(onboarding_endpoints.router)
api_router.include_router(document_endpoints.router)
api_router.include_router(dashboard_endpoints.router)
api_router.include_router(logs_endpoints.router)
