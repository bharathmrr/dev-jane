"""FastAPI application factory and entrypoint."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger, request_id_ctx

configure_logging()
log = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", env=settings.ENV, app=settings.APP_NAME)
    yield
    try:
        from app.core.redis_client import get_redis
        await get_redis().aclose()
    except Exception:
        pass
    log.info("shutdown")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["X-Request-ID"] = rid
        return response


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.ENV != "prod" else None,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.ENV != "prod" else [],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    if settings.PROMETHEUS_ENABLED:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator().instrument(app).expose(app, endpoint="/metrics")

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard():
        return HTMLResponse(_DASHBOARD_HTML)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # pragma: no cover
        log.error("unhandled_exception", path=request.url.path, error=str(exc))
        return JSONResponse(status_code=500, content={"detail": "Internal error"})

    return app


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jane Aerospace — Pipeline</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       background:#f1f5f9;display:flex;min-height:100vh}
  /* Sidebar */
  .sidebar{width:220px;min-height:100vh;background:#0f172a;display:flex;
            flex-direction:column;padding:0;flex-shrink:0}
  .sidebar-logo{padding:24px 20px 20px;border-bottom:1px solid #1e293b}
  .sidebar-logo .brand{font-size:16px;font-weight:700;color:#fff;letter-spacing:.4px}
  .sidebar-logo .sub{font-size:10px;color:#64748b;letter-spacing:.5px;margin-top:3px}
  .sidebar-nav{padding:16px 0}
  .nav-item{display:flex;align-items:center;gap:10px;padding:10px 20px;
             font-size:13px;color:#94a3b8;cursor:pointer;border-radius:0;
             transition:background .15s,color .15s}
  .nav-item.active,.nav-item:hover{background:#1e293b;color:#fff}
  .nav-item svg{width:16px;height:16px;flex-shrink:0}
  .sidebar-footer{margin-top:auto;padding:16px 20px;border-top:1px solid #1e293b;
                  font-size:11px;color:#475569}
  /* Main */
  .main{flex:1;display:flex;flex-direction:column;overflow:hidden}
  /* Top bar */
  .topbar{background:#fff;border-bottom:1px solid #e2e8f0;padding:14px 28px;
           display:flex;align-items:center;justify-content:space-between}
  .topbar-title{font-size:18px;font-weight:700;color:#0f172a}
  .topbar-actions{display:flex;gap:10px}
  .btn{padding:8px 16px;border-radius:6px;font-size:13px;font-weight:600;
       cursor:pointer;border:none;transition:background .15s}
  .btn-primary{background:#3b82f6;color:#fff}
  .btn-primary:hover{background:#2563eb}
  .btn-secondary{background:#f1f5f9;color:#374151;border:1px solid #e2e8f0}
  .btn-secondary:hover{background:#e2e8f0}

  /* Stats bar */
  .stats-bar{display:flex;gap:16px;padding:20px 28px 0}
  .stat-card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;
              padding:16px 20px;flex:1;min-width:0}
  .stat-label{font-size:11px;color:#64748b;font-weight:600;
               text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
  .stat-value{font-size:28px;font-weight:700;color:#0f172a}
  .stat-sub{font-size:12px;color:#94a3b8;margin-top:3px}
  /* Board */
  .board{display:flex;gap:16px;padding:20px 28px 28px;overflow-x:auto;flex:1;align-items:flex-start}
  .column{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;
           width:260px;flex-shrink:0;display:flex;flex-direction:column;max-height:calc(100vh - 220px)}
  .col-header{padding:14px 16px;border-bottom:1px solid #e2e8f0;display:flex;
               align-items:center;justify-content:space-between}
  .col-title{font-size:13px;font-weight:700;color:#374151;display:flex;align-items:center;gap:8px}
  .col-dot{width:9px;height:9px;border-radius:50%}
  .col-count{font-size:12px;color:#94a3b8;font-weight:600;background:#e2e8f0;
              padding:2px 8px;border-radius:12px}
  .col-body{padding:12px;overflow-y:auto;flex:1;display:flex;flex-direction:column;gap:10px}
  /* Cards */
  .card{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px 16px;
         box-shadow:0 1px 3px rgba(0,0,0,.04);transition:box-shadow .15s,transform .1s}
  .card:hover{box-shadow:0 4px 12px rgba(0,0,0,.08);transform:translateY(-1px)}
  .card-name{font-size:14px;font-weight:700;color:#0f172a;margin-bottom:4px;
              white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .card-email{font-size:12px;color:#64748b;margin-bottom:10px;
               white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .card-meta{display:flex;flex-wrap:wrap;gap:6px}
  .badge{font-size:11px;padding:3px 8px;border-radius:12px;font-weight:500}
  .badge-new{background:#f0fdf4;color:#166534;border:1px solid #bbf7d0}
  .badge-sent{background:#eff6ff;color:#1e40af;border:1px solid #bfdbfe}
  .badge-replied{background:#fef9c3;color:#854d0e;border:1px solid #fde68a}
  .badge-booked{background:#f0fdf4;color:#166534;border:1px solid #86efac}
  .card-slot{font-size:11px;color:#475569;margin-top:8px;padding-top:8px;
              border-top:1px solid #f1f5f9;white-space:nowrap;overflow:hidden;
              text-overflow:ellipsis}
  .card-link a{font-size:11px;color:#3b82f6;text-decoration:none}
  .card-link a:hover{text-decoration:underline}
  .empty{text-align:center;padding:28px 16px;color:#94a3b8;font-size:13px}
  /* Spinner */
  .spinner{display:inline-block;width:18px;height:18px;border:2px solid #e2e8f0;
            border-top-color:#3b82f6;border-radius:50%;animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .loading{display:flex;justify-content:center;padding:40px}
</style>
</head>
<body>
<!-- Sidebar -->
<aside class="sidebar">
  <div class="sidebar-logo">
    <div class="brand">Jane Aerospace</div>
    <div class="sub">LEAD PIPELINE</div>
  </div>
  <nav class="sidebar-nav">
    <div class="nav-item active">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
        <rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>
      </svg>
      Pipeline
    </div>
    <div class="nav-item" onclick="window.open('/docs','_blank')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/>
      </svg>
      API Docs
    </div>
  </nav>
  <div class="sidebar-footer">Auto-refreshes every 30s</div>
</aside>

<!-- Main -->
<main class="main">
  <div class="topbar">
    <div class="topbar-title">Deals Pipeline</div>
    <div class="topbar-actions">
      <button class="btn btn-secondary" onclick="loadData()">&#x21bb; Refresh</button>
      <button class="btn btn-primary" onclick="triggerSync()">+ Sync Sheet</button>
      <button class="btn btn-primary" onclick="triggerEmail()">&#x2709; Send Emails</button>
    </div>
  </div>

  <!-- Stats -->
  <div class="stats-bar" id="stats-bar">
    <div class="stat-card"><div class="stat-label">Total Leads</div>
      <div class="stat-value" id="s-total">—</div></div>
    <div class="stat-card"><div class="stat-label">Emails Sent</div>
      <div class="stat-value" id="s-sent">—</div></div>
    <div class="stat-card"><div class="stat-label">Replied</div>
      <div class="stat-value" id="s-replied">—</div></div>
    <div class="stat-card"><div class="stat-label">Booked</div>
      <div class="stat-value" id="s-booked">—</div></div>
    <div class="stat-card"><div class="stat-label">Conversion</div>
      <div class="stat-value" id="s-conv">—</div>
      <div class="stat-sub">leads → booked</div></div>
  </div>

  <!-- Kanban Board -->
  <div class="board">
    <div class="column">
      <div class="col-header">
        <div class="col-title">
          <div class="col-dot" style="background:#94a3b8"></div>New
        </div>
        <span class="col-count" id="cnt-new">0</span>
      </div>
      <div class="col-body" id="col-new"><div class="loading"><div class="spinner"></div></div></div>
    </div>
    <div class="column">
      <div class="col-header">
        <div class="col-title">
          <div class="col-dot" style="background:#3b82f6"></div>Email Sent
        </div>
        <span class="col-count" id="cnt-sent">0</span>
      </div>
      <div class="col-body" id="col-sent"><div class="loading"><div class="spinner"></div></div></div>
    </div>
    <div class="column">
      <div class="col-header">
        <div class="col-title">
          <div class="col-dot" style="background:#f59e0b"></div>Replied
        </div>
        <span class="col-count" id="cnt-replied">0</span>
      </div>
      <div class="col-body" id="col-replied"><div class="loading"><div class="spinner"></div></div></div>
    </div>
    <div class="column">
      <div class="col-header">
        <div class="col-title">
          <div class="col-dot" style="background:#22c55e"></div>Booked
        </div>
        <span class="col-count" id="cnt-booked">0</span>
      </div>
      <div class="col-body" id="col-booked"><div class="loading"><div class="spinner"></div></div></div>
    </div>
  </div>
</main>

<script>
const BASE = '/api/v1/v2';

function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString('en-IN', {
    day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'
  });
}

function card(lead) {
  const statusBadge = {
    new: '<span class="badge badge-new">New</span>',
    sent: '<span class="badge badge-sent">Sent</span>',
    replied: '<span class="badge badge-replied">Replied</span>',
    booked: '<span class="badge badge-booked">&#x2713; Booked</span>',
  }[lead.status] || '';

  const slotLine = lead.selected_slot
    ? `<div class="card-slot">&#x1F4C5; ${lead.selected_slot}</div>` : '';
  const linkLine = lead.zoho_meeting_link
    ? `<div class="card-link" style="margin-top:6px">
         <a href="${lead.zoho_meeting_link}" target="_blank">&#x1F517; Join Meeting</a>
       </div>` : '';
  const sentLine = lead.sent_at
    ? `<span class="badge" style="background:#f1f5f9;color:#64748b;border:1px solid #e2e8f0">
         Sent ${fmtDate(lead.sent_at)}</span>` : '';

  return `<div class="card">
    <div class="card-name" title="${lead.business_name}">${lead.business_name}</div>
    <div class="card-email" title="${lead.email}">${lead.email}</div>
    <div class="card-meta">${statusBadge}${sentLine}</div>
    ${slotLine}${linkLine}
  </div>`;
}

async function loadData() {
  try {
    const [leads, stats] = await Promise.all([
      fetch(BASE + '/leads').then(r => r.json()),
      fetch(BASE + '/dashboard/stats').then(r => r.json()),
    ]);

    // Stats
    document.getElementById('s-total').textContent = stats.total_leads;
    document.getElementById('s-sent').textContent = stats.sent_leads;
    document.getElementById('s-replied').textContent = stats.replied_leads;
    document.getElementById('s-booked').textContent = stats.booked_leads;
    document.getElementById('s-conv').textContent = stats.conversion_rate + '%';

    // Group
    const groups = {new:[], sent:[], replied:[], booked:[]};
    leads.forEach(l => { if (groups[l.status]) groups[l.status].push(l); });

    ['new','sent','replied','booked'].forEach(col => {
      const items = groups[col];
      document.getElementById('cnt-' + col).textContent = items.length;
      document.getElementById('col-' + col).innerHTML =
        items.length ? items.map(card).join('') :
        '<div class="empty">No leads</div>';
    });
  } catch(e) {
    console.error('Failed to load pipeline data', e);
  }
}

async function triggerSync() {
  await fetch(BASE + '/sync-sheet', {method:'POST'});
  alert('Google Sheet sync triggered!');
}

async function triggerEmail() {
  await fetch(BASE + '/send-email', {method:'POST'});
  alert('Email send triggered! Check leads in ~30s.');
  setTimeout(loadData, 5000);
}

loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>"""


app = create_app()
