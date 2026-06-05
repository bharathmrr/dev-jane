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
  .sidebar{width:220px;min-height:100vh;background:#0f172a;display:flex;
            flex-direction:column;padding:0;flex-shrink:0}
  .sidebar-logo{padding:24px 20px 20px;border-bottom:1px solid #1e293b}
  .sidebar-logo .brand{font-size:16px;font-weight:700;color:#fff;letter-spacing:.4px}
  .sidebar-logo .sub{font-size:10px;color:#64748b;letter-spacing:.5px;margin-top:3px}
  .sidebar-nav{padding:16px 0}
  .nav-item{display:flex;align-items:center;gap:10px;padding:10px 20px;font-size:13px;
             color:#94a3b8;cursor:pointer;transition:background .15s,color .15s}
  .nav-item.active,.nav-item:hover{background:#1e293b;color:#fff}
  .nav-item svg{width:16px;height:16px;flex-shrink:0}
  .sidebar-footer{margin-top:auto;padding:16px 20px;border-top:1px solid #1e293b;
                  font-size:11px;color:#475569}
  .main{flex:1;display:flex;flex-direction:column;overflow:hidden}
  .topbar{background:#fff;border-bottom:1px solid #e2e8f0;padding:14px 28px;
           display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
  .topbar-title{font-size:18px;font-weight:700;color:#0f172a}
  .topbar-actions{display:flex;gap:10px}
  .btn{padding:8px 16px;border-radius:6px;font-size:13px;font-weight:600;
       cursor:pointer;border:none;transition:background .15s}
  .btn-primary{background:#3b82f6;color:#fff}
  .btn-primary:hover{background:#2563eb}
  .btn-secondary{background:#f1f5f9;color:#374151;border:1px solid #e2e8f0}
  .btn-secondary:hover{background:#e2e8f0}
  .btn-success{background:#16a34a;color:#fff;font-size:12px;padding:6px 12px}
  .btn-success:hover{background:#15803d}
  .btn-approve{background:#16a34a;color:#fff;padding:6px 14px;font-size:12px}
  .btn-approve:hover{background:#15803d}
  .btn-reject{background:#dc2626;color:#fff;padding:6px 14px;font-size:12px}
  .btn-reject:hover{background:#b91c1c}
  .stats-bar{display:flex;gap:16px;padding:20px 28px 0;flex-shrink:0}
  .stat-card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;
              padding:16px 20px;flex:1;min-width:0}
  .stat-label{font-size:11px;color:#64748b;font-weight:600;
               text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
  .stat-value{font-size:28px;font-weight:700;color:#0f172a}
  .stat-sub{font-size:12px;color:#94a3b8;margin-top:3px}
  /* Tab nav */
  .tab-nav{display:flex;gap:0;padding:16px 28px 0;flex-shrink:0}
  .tab-btn{padding:10px 20px;font-size:13px;font-weight:600;cursor:pointer;
            border:1px solid #e2e8f0;background:#fff;color:#64748b;
            border-bottom:none;border-radius:8px 8px 0 0;margin-right:4px;transition:all .15s}
  .tab-btn.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
  .tab-panel{display:none;flex:1;overflow:auto}
  .tab-panel.active{display:flex;flex-direction:column}
  /* Kanban board */
  .board{display:flex;gap:16px;padding:20px 28px 28px;overflow-x:auto;flex:1;align-items:flex-start}
  .column{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;
           width:270px;flex-shrink:0;display:flex;flex-direction:column;max-height:calc(100vh - 300px)}
  .col-header{padding:14px 16px;border-bottom:1px solid #e2e8f0;display:flex;
               align-items:center;justify-content:space-between}
  .col-title{font-size:13px;font-weight:700;color:#374151;display:flex;align-items:center;gap:8px}
  .col-dot{width:9px;height:9px;border-radius:50%}
  .col-count{font-size:12px;color:#94a3b8;font-weight:600;background:#e2e8f0;
              padding:2px 8px;border-radius:12px}
  .col-body{padding:12px;overflow-y:auto;flex:1;display:flex;flex-direction:column;gap:10px}
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
              border-top:1px solid #f1f5f9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .card-link a{font-size:11px;color:#3b82f6;text-decoration:none}
  .card-link a:hover{text-decoration:underline}
  .empty{text-align:center;padding:28px 16px;color:#94a3b8;font-size:13px}
  .spinner{display:inline-block;width:18px;height:18px;border:2px solid #e2e8f0;
            border-top-color:#3b82f6;border-radius:50%;animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .loading{display:flex;justify-content:center;padding:40px}

  /* Onboarding tab */
  .ob-table{width:100%;border-collapse:collapse;font-size:13px}
  .ob-table th{background:#f8fafc;padding:10px 14px;text-align:left;font-weight:600;
                color:#374151;border-bottom:2px solid #e2e8f0;font-size:12px;
                text-transform:uppercase;letter-spacing:.4px}
  .ob-table td{padding:12px 14px;border-bottom:1px solid #f1f5f9;vertical-align:top;color:#374151}
  .ob-table tr:hover td{background:#f8fafc}
  .status-pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}
  .s-pending{background:#f1f5f9;color:#64748b}
  .s-sent{background:#eff6ff;color:#1e40af}
  .s-submitted{background:#fef9c3;color:#854d0e}
  .s-review{background:#fff7ed;color:#c2410c}
  .s-rejected{background:#fef2f2;color:#dc2626}
  .s-approved{background:#f0fdf4;color:#16a34a}
  .s-proceed{background:#f0fdf4;color:#166534;font-weight:700}
  .action-btns{display:flex;gap:6px;flex-wrap:wrap}
  .ob-detail{font-size:11px;color:#94a3b8;margin-top:3px}
  /* Modal */
  .modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);
                   z-index:1000;align-items:center;justify-content:center}
  .modal-backdrop.open{display:flex}
  .modal{background:#fff;border-radius:12px;padding:32px;width:560px;max-width:95vw;
          max-height:85vh;overflow-y:auto;position:relative}
  .modal h3{font-size:16px;font-weight:700;color:#0f172a;margin-bottom:16px}
  .modal textarea{width:100%;padding:10px 12px;border:1px solid #e2e8f0;border-radius:6px;
                   font-size:13px;font-family:inherit;resize:vertical;min-height:100px}
  .modal .doc-preview{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
                        padding:16px;font-size:13px;white-space:pre-wrap;max-height:300px;
                        overflow-y:auto;font-family:Georgia,serif;line-height:1.7;margin:12px 0}
  .modal-actions{display:flex;gap:10px;margin-top:16px;justify-content:flex-end}
  .modal-close{position:absolute;top:16px;right:16px;background:none;border:none;
                font-size:20px;cursor:pointer;color:#94a3b8;line-height:1}
  .modal-close:hover{color:#374151}
  .ob-section{padding:20px 28px;flex:1;overflow:auto}
  .ob-section h2{font-size:16px;font-weight:700;color:#0f172a;margin-bottom:16px}
</style>
</head>
<body>
<aside class="sidebar">
  <div class="sidebar-logo">
    <div class="brand">Jane Aerospace</div>
    <div class="sub">LEAD PIPELINE</div>
  </div>
  <nav class="sidebar-nav">
    <div class="nav-item active" onclick="showTab('pipeline',this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
        <rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>
      </svg>Pipeline
    </div>
    <div class="nav-item" onclick="showTab('onboarding',this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/>
        <circle cx="9" cy="7" r="4"/>
        <path d="M22 21v-2a4 4 0 0 0-3-3.87"/>
        <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
      </svg>Onboarding
    </div>
    <div class="nav-item" onclick="window.open('/docs','_blank')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/>
      </svg>API Docs
    </div>
  </nav>
  <div class="sidebar-footer">Auto-refreshes every 30s</div>
</aside>

<main class="main">
  <div class="topbar">
    <div class="topbar-title" id="page-title">Deals Pipeline</div>
    <div class="topbar-actions">
      <button class="btn btn-secondary" onclick="loadAll()">&#x21bb; Refresh</button>
      <button class="btn btn-primary" onclick="triggerSync()">+ Sync Sheet</button>
      <button class="btn btn-primary" onclick="triggerEmail()">&#x2709; Send Emails</button>
    </div>
  </div>

  <!-- Stats -->
  <div class="stats-bar">
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

  <!-- Pipeline tab -->
  <div id="tab-pipeline" class="tab-panel active">
    <div class="board">
      <div class="column">
        <div class="col-header">
          <div class="col-title"><div class="col-dot" style="background:#94a3b8"></div>New</div>
          <span class="col-count" id="cnt-new">0</span>
        </div>
        <div class="col-body" id="col-new"><div class="loading"><div class="spinner"></div></div></div>
      </div>
      <div class="column">
        <div class="col-header">
          <div class="col-title"><div class="col-dot" style="background:#3b82f6"></div>Email Sent</div>
          <span class="col-count" id="cnt-sent">0</span>
        </div>
        <div class="col-body" id="col-sent"><div class="loading"><div class="spinner"></div></div></div>
      </div>
      <div class="column">
        <div class="col-header">
          <div class="col-title"><div class="col-dot" style="background:#f59e0b"></div>Replied</div>
          <span class="col-count" id="cnt-replied">0</span>
        </div>
        <div class="col-body" id="col-replied"><div class="loading"><div class="spinner"></div></div></div>
      </div>
      <div class="column">
        <div class="col-header">
          <div class="col-title"><div class="col-dot" style="background:#22c55e"></div>Booked</div>
          <span class="col-count" id="cnt-booked">0</span>
        </div>
        <div class="col-body" id="col-booked"><div class="loading"><div class="spinner"></div></div></div>
      </div>
    </div>
  </div>

  <!-- Onboarding tab -->
  <div id="tab-onboarding" class="tab-panel">
    <div class="ob-section">
      <h2>Customer Onboarding Pipeline</h2>
      <div id="ob-table-wrap"><div class="loading"><div class="spinner"></div></div></div>
    </div>
  </div>
</main>

<!-- Review Modal -->
<div class="modal-backdrop" id="modal">
  <div class="modal">
    <button class="modal-close" onclick="closeModal()">&#x2715;</button>
    <h3 id="modal-title">Review</h3>
    <div id="modal-body"></div>
    <div class="modal-actions" id="modal-actions"></div>
  </div>
</div>

<script>
const BASE = '/api/v1/v2';
const OB  = '/api/v1/onboarding';
let _onboardingMap = {}; // lead_id -> onboarding record

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------
function showTab(name, el) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (el) el.classList.add('active');
  document.getElementById('page-title').textContent =
    name === 'onboarding' ? 'Customer Onboarding' : 'Deals Pipeline';
  if (name === 'onboarding') loadOnboarding();
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString('en-IN',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
}

function statusPill(val) {
  if (!val) return '<span class="status-pill s-pending">Pending</span>';
  const v = val.toUpperCase();
  const cls = v.includes('APPROVED') ? 's-approved'
    : v.includes('PROCEED') ? 's-proceed'
    : v.includes('REJECTED') ? 's-rejected'
    : v.includes('REVIEW') || v.includes('UNDER') ? 's-review'
    : v.includes('SUBMITTED') ? 's-submitted'
    : v.includes('SENT') ? 's-sent'
    : 's-pending';
  const label = val.replace(/_/g,' ');
  return `<span class="status-pill ${cls}">${label}</span>`;
}

// ---------------------------------------------------------------------------
// Pipeline cards
// ---------------------------------------------------------------------------
function card(lead) {
  const ob = _onboardingMap[lead.id];
  const statusBadge = {
    new:'<span class="badge badge-new">New</span>',
    sent:'<span class="badge badge-sent">Sent</span>',
    replied:'<span class="badge badge-replied">Replied</span>',
    booked:'<span class="badge badge-booked">&#x2713; Booked</span>',
  }[lead.status] || '';

  const slotLine = lead.selected_slot
    ? `<div class="card-slot">&#x1F4C5; ${lead.selected_slot}</div>` : '';
  const linkLine = lead.zoho_meeting_link
    ? `<div class="card-link" style="margin-top:6px"><a href="${lead.zoho_meeting_link}" target="_blank">&#x1F517; Join Meeting</a></div>` : '';
  const sentLine = lead.sent_at
    ? `<span class="badge" style="background:#f1f5f9;color:#64748b;border:1px solid #e2e8f0">Sent ${fmtDate(lead.sent_at)}</span>` : '';

  let onboardingBtn = '';
  let obStatus = '';
  if (lead.status === 'booked') {
    if (ob) {
      obStatus = `<div style="margin-top:10px;padding-top:10px;border-top:1px solid #f1f5f9;font-size:11px;">
        <div>KYC: ${statusPill(ob.kyc_status)}</div>
        <div style="margin-top:4px;">NDA: ${statusPill(ob.nda_status)}</div>
        <div style="margin-top:4px;">Agreement: ${statusPill(ob.agreement_status)}</div>
      </div>`;
    } else {
      onboardingBtn = `<div style="margin-top:10px;">
        <button class="btn btn-success" onclick="startOnboarding('${lead.id}','${lead.business_name}')">
          &#x25B6; Start Customer Onboarding
        </button>
      </div>`;
    }
  }

  return `<div class="card" id="lcard-${lead.id}">
    <div class="card-name" title="${lead.business_name}">${lead.business_name}</div>
    <div class="card-email" title="${lead.email}">${lead.email}</div>
    <div class="card-meta">${statusBadge}${sentLine}</div>
    ${slotLine}${linkLine}${obStatus}${onboardingBtn}
  </div>`;
}

// ---------------------------------------------------------------------------
// Pipeline load
// ---------------------------------------------------------------------------
async function loadPipeline() {
  try {
    const [leads, stats, obList] = await Promise.all([
      fetch(BASE + '/leads').then(r => r.json()),
      fetch(BASE + '/dashboard/stats').then(r => r.json()),
      fetch(OB + '/list').then(r => r.json()).catch(() => []),
    ]);

    _onboardingMap = {};
    (obList || []).forEach(o => { _onboardingMap[o.lead_id] = o; });

    document.getElementById('s-total').textContent = stats.total_leads;
    document.getElementById('s-sent').textContent = stats.sent_leads;
    document.getElementById('s-replied').textContent = stats.replied_leads;
    document.getElementById('s-booked').textContent = stats.booked_leads;
    document.getElementById('s-conv').textContent = stats.conversion_rate + '%';

    const groups = {new:[],sent:[],replied:[],booked:[]};
    leads.forEach(l => { if (groups[l.status]) groups[l.status].push(l); });

    ['new','sent','replied','booked'].forEach(col => {
      const items = groups[col];
      document.getElementById('cnt-' + col).textContent = items.length;
      document.getElementById('col-' + col).innerHTML =
        items.length ? items.map(card).join('') : '<div class="empty">No leads</div>';
    });
  } catch(e) { console.error('Pipeline load failed', e); }
}

// ---------------------------------------------------------------------------
// Onboarding table
// ---------------------------------------------------------------------------
async function loadOnboarding() {
  document.getElementById('ob-table-wrap').innerHTML =
    '<div class="loading"><div class="spinner"></div></div>';
  try {
    const recs = await fetch(OB + '/list').then(r => r.json());
    if (!recs.length) {
      document.getElementById('ob-table-wrap').innerHTML =
        '<div class="empty" style="padding:40px;text-align:center;color:#94a3b8;">No onboarding records yet.</div>';
      return;
    }
    let rows = recs.map(r => {
      const kycActions = buildKycActions(r);
      const ndaActions = buildNdaActions(r);
      const agActions  = buildAgActions(r);
      return `<tr id="ob-row-${r.id}">
        <td><strong>${r.lead_business_name||'—'}</strong>
          <div class="ob-detail">${r.lead_email||''}</div>
          <div class="ob-detail">${r.lead_contact_name||''}</div>
        </td>
        <td><span class="status-pill s-pending" style="background:#f0fdf4;color:#166534;">
          ${(r.company_type||'—').toUpperCase()}</span></td>
        <td>
          ${statusPill(r.kyc_status)}
          <div class="ob-detail">${r.kyc_status_display||''}</div>
          <div class="ob-detail">Follow-ups: ${r.kyc_followup_count||0}</div>
          <div class="action-btns" style="margin-top:6px;">${kycActions}</div>
        </td>
        <td>
          ${statusPill(r.nda_status)}
          <div class="ob-detail">${r.nda_status_display||''}</div>
          <div class="ob-detail">Follow-ups: ${r.nda_followup_count||0}</div>
          <div class="action-btns" style="margin-top:6px;">${ndaActions}</div>
        </td>
        <td>
          ${statusPill(r.agreement_status)}
          <div class="ob-detail">${r.agreement_status_display||''}</div>
          <div class="ob-detail">Follow-ups: ${r.agreement_followup_count||0}</div>
          <div class="action-btns" style="margin-top:6px;">${agActions}</div>
        </td>
      </tr>`;
    }).join('');

    document.getElementById('ob-table-wrap').innerHTML = `
      <table class="ob-table">
        <thead><tr>
          <th>Company</th><th>Type</th>
          <th>KYC Status</th><th>NDA Status</th><th>Agreement Status</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch(e) {
    document.getElementById('ob-table-wrap').innerHTML =
      '<div class="empty" style="padding:40px;color:#dc2626;">Failed to load onboarding data.</div>';
    console.error(e);
  }
}

function buildKycActions(r) {
  if (r.kyc_status === 'UNDER_REVIEW') {
    return `<button class="btn btn-approve" onclick="kycReview('${r.id}','approve')">&#x2713; Approve</button>
            <button class="btn btn-reject"  onclick="kycReview('${r.id}','reject')">&#x2715; Reject</button>`;
  }
  if (r.kyc_status === 'APPROVED') return '<span style="color:#16a34a;font-size:12px;">&#x2713; Approved</span>';
  return '';
}

function buildNdaActions(r) {
  if (r.nda_status === 'TEAM_REVIEW') {
    return `<button class="btn btn-approve" onclick="showDocReview('${r.id}','nda','draft')">Preview &amp; Review</button>`;
  }
  if (r.nda_status === 'SIGN_UNDER_REVIEW') {
    return `<button class="btn btn-approve" onclick="showDocReview('${r.id}','nda','sign')">Review Signed</button>`;
  }
  if (r.nda_status === 'APPROVED') return '<span style="color:#16a34a;font-size:12px;">&#x2713; NDA Done</span>';
  if (r.nda_status === 'PROCEED_NEXT') return '<span style="color:#16a34a;font-size:12px;">&#x2713; Proceed</span>';
  return '';
}

function buildAgActions(r) {
  if (r.agreement_status === 'TEAM_REVIEW') {
    return `<button class="btn btn-approve" onclick="showDocReview('${r.id}','agreement','draft')">Preview &amp; Review</button>`;
  }
  if (r.agreement_status === 'SIGN_UNDER_REVIEW') {
    return `<button class="btn btn-approve" onclick="showDocReview('${r.id}','agreement','sign')">Review Signed</button>`;
  }
  if (r.agreement_status === 'APPROVED') return '<span style="color:#16a34a;font-size:12px;">&#x2713; Agreement Done</span>';
  if (r.agreement_status === 'PROCEED_NEXT') return '<span style="color:#16a34a;font-size:12px;">&#x1F389; Proceed to Training</span>';
  return '';
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
async function startOnboarding(leadId, name) {
  if (!confirm(`Start customer onboarding for ${name}?\\n\\nThis will detect company type and send the KYC form.`)) return;
  const res = await fetch(`${OB}/start/${leadId}`, {method:'POST'});
  const data = await res.json();
  alert(data.message || 'Onboarding initiated!');
  loadAll();
}

async function kycReview(obId, action) {
  let notes = '';
  if (action === 'reject') {
    notes = prompt('Enter rejection reason (required):');
    if (!notes) return;
  }
  const res = await fetch(`${OB}/kyc/review/${obId}`, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action, notes}),
  });
  const data = await res.json();
  alert(data.message);
  loadAll();
}

async function showDocReview(obId, doc, mode) {
  const endpoint = mode === 'sign'
    ? `${OB}/${doc}/signed/${obId}`
    : `${OB}/${doc}/preview/${obId}`;
  const data = await fetch(endpoint).then(r => r.json());

  const content = mode === 'sign'
    ? (data.signed_file_url
        ? `<p>Signed document received at ${data.signed_received_at||''}.</p>
           <p><a href="${data.signed_file_url}" target="_blank">&#x1F4CE; View Signed Document in Zoho WorkDrive</a></p>`
        : '<p style="color:#dc2626;">Signed document file not available yet.</p>')
    : `<div class="doc-preview">${(data.nda_draft_content||data.agreement_draft_content||'Draft not available yet.')}</div>`;

  const titleMap = {
    nda_draft: 'Review NDA Draft', nda_sign: 'Review Signed NDA',
    agreement_draft: 'Review Agreement Draft', agreement_sign: 'Review Signed Agreement',
  };
  document.getElementById('modal-title').textContent = titleMap[`${doc}_${mode}`] || 'Review';
  document.getElementById('modal-body').innerHTML = `
    ${content}
    <label style="display:block;margin-top:12px;font-size:13px;font-weight:600;color:#374151;">
      Notes / Feedback (required if rejecting):
    </label>
    <textarea id="review-notes" placeholder="Enter notes for the team or rejection reason..."></textarea>
  `;
  document.getElementById('modal-actions').innerHTML = `
    <button class="btn btn-approve" onclick="submitDocReview('${obId}','${doc}','${mode}','approve')">
      &#x2713; Approve
    </button>
    <button class="btn btn-reject" onclick="submitDocReview('${obId}','${doc}','${mode}','reject')">
      &#x2715; Reject
    </button>
  `;
  document.getElementById('modal').classList.add('open');
}

async function submitDocReview(obId, doc, mode, action) {
  const notes = document.getElementById('review-notes').value.trim();
  if (action === 'reject' && !notes) { alert('Please enter rejection notes.'); return; }

  const endpoint = mode === 'sign'
    ? `${OB}/${doc}/sign-review/${obId}`
    : `${OB}/${doc}/draft-review/${obId}`;

  const res = await fetch(endpoint, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action, notes}),
  });
  const data = await res.json();
  closeModal();
  alert(data.message || 'Done!');
  loadAll();
}

function closeModal() {
  document.getElementById('modal').classList.remove('open');
}
document.getElementById('modal').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------
async function loadAll() {
  await loadPipeline();
}

async function triggerSync() {
  await fetch(BASE + '/sync-sheet', {method:'POST'});
  alert('Google Sheet sync triggered!');
}

async function triggerEmail() {
  await fetch(BASE + '/send-email', {method:'POST'});
  alert('Email send triggered! Check leads in ~30s.');
  setTimeout(loadAll, 5000);
}

loadAll();
setInterval(loadAll, 30000);
</script>
</body>
</html>"""


app = create_app()
