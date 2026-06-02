from fastapi import APIRouter

from app.api.v1 import auth, bookings, system

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(system.router)   # /bookings/list must come before /bookings/{id}
api_router.include_router(bookings.router)
