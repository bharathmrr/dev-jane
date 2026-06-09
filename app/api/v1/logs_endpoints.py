"""API endpoints to read pipeline and backend log files."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Query

router = APIRouter(prefix="/logs", tags=["logs"])

_LOG_DIR = Path("logs")
_PIPELINE_LOG = _LOG_DIR / "pipeline.log"
_BACKEND_LOG = _LOG_DIR / "backend.log"

# Line format written by pipeline_logger.py:
# "2026-06-08 14:23:11 IST  [EVENT_TYPE                    ]  company  email  detail"
_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} IST)\s+\[([^\]]+)\]\s{2,}(.*?)\s{2,}(.*?)\s{2,}(.*)$"
)

# Status tag per event (mirrors pipeline_logger.EVENT_STATUS)
_STATUS: dict[str, str] = {
    "KYC_AUTO_APPROVED": "ok", "KYC_MANUALLY_APPROVED": "ok",
    "NDA_SIGNED_RECEIVED": "ok", "AGREEMENT_SIGNED_RECEIVED": "ok",
    "NDA_APPROVED": "ok", "AGREEMENT_APPROVED": "ok", "BOOKING_COMPLETED": "ok",
    "KYC_MANUAL_REVIEW": "warn", "KYC_ISSUES_FOUND": "warn",
    "NDA_SIGN_REJECTED": "warn", "AGREEMENT_SIGN_REJECTED": "warn",
    "KYC_REJECTED": "error", "ERROR": "error",
}


def _parse_line(line: str) -> dict:
    m = _LINE_RE.match(line.strip())
    if m:
        event = m.group(2).strip()
        return {
            "ts": m.group(1).strip(),
            "event": event,
            "company": m.group(3).strip().rstrip("—").strip() or "—",
            "email": m.group(4).strip().rstrip("—").strip() or "—",
            "detail": m.group(5).strip(),
            "status": _STATUS.get(event, "info"),
        }
    return {
        "ts": "", "event": "LOG", "company": "—", "email": "—",
        "detail": line.strip(), "status": "info",
    }


def _tail(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-limit:]


@router.get("/pipeline")
async def get_pipeline_logs(limit: int = Query(default=150, le=500)):
    """Return latest pipeline events parsed as JSON (newest first)."""
    lines = _tail(_PIPELINE_LOG, limit)
    total_path = _PIPELINE_LOG
    total = 0
    if total_path.exists():
        all_lines = total_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        total = sum(1 for l in all_lines if l.strip())

    events = [_parse_line(l) for l in reversed(lines)]
    return {"events": events, "shown": len(events), "total": total}


@router.get("/backend")
async def get_backend_logs(limit: int = Query(default=100, le=500)):
    """Return latest backend.log lines (newest first)."""
    lines = _tail(_BACKEND_LOG, limit)
    return {"lines": list(reversed(lines)), "shown": len(lines)}
