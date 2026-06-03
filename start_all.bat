@echo off
echo Starting Backend API...
start cmd /k ".venv\Scripts\activate && uvicorn app.main:app --reload --port 8000"

echo Starting Celery Worker...
start cmd /k ".venv\Scripts\activate && celery -A app.workers.celery_app worker --loglevel=info --pool=solo"

echo Starting Celery Scheduler (Beat)...
start cmd /k ".venv\Scripts\activate && celery -A app.workers.celery_app beat --loglevel=info"

echo All services started in separate windows!
