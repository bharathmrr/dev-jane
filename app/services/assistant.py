"""Voice co-pilot — a READ-ONLY agent over the dashboard's pipeline data.

The assistant can only (a) read data through a fixed set of query tools and
(b) emit a single navigation directive for the browser to execute. There is
deliberately **no tool that mutates state, sends email, or approves anything**,
so even a hostile or confused prompt cannot take an action.

The tool-use loop is provider-agnostic: it runs against the real Anthropic client
when a key is configured, and against the deterministic ``StubClient`` otherwise
(which keyword-routes a single tool call), so the feature degrades gracefully and
stays unit-testable.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import LeadV2, OnboardingRecord, User
from app.services.llm_client import LLMClient, get_llm_client

log = get_logger(__name__)

MAX_TOOL_TURNS = 6          # hard guard on the tool-use loop
TOOL_RESULT_CAP = 6000      # chars of any single tool result fed back to the model

_NAV_TARGETS = {"navigate", "open_lead", "open_comments", "open_notifications", "filter_board"}
_VIEWS = ("overview", "kanban", "leads", "sheet", "bookings", "docs", "approvals", "users")

# Anthropic tool schema — every tool here is read-only or a UI directive.
TOOLS: list[dict] = [
    {
        "name": "search_leads",
        "description": "Search leads by company name, email, or contact name. "
                       "Returns matching leads with their pipeline stage, status and booked time.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "company, email or contact text"}},
            "required": ["query"],
        },
    },
    {
        "name": "pipeline_summary",
        "description": "High-level pipeline numbers: total leads, booked count, onboarding count, "
                       "pending approvals, and a breakdown by stage.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_pending",
        "description": "Items needing attention. kind='approvals' for things awaiting the team's "
                       "decision (KYC/NDA/Agreement); kind='comments' for open change-requests from leads.",
        "input_schema": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["approvals", "comments"]}},
            "required": ["kind"],
        },
    },
    {
        "name": "lead_detail",
        "description": "Full onboarding status (KYC, NDA, Agreement, follow-ups) for one lead, by name or id.",
        "input_schema": {
            "type": "object",
            "properties": {"name_or_id": {"type": "string"}},
            "required": ["name_or_id"],
        },
    },
    {
        "name": "navigate",
        "description": "Move the dashboard UI for the user. Use whenever they ask to see/open/show something. "
                       "Returns control to the browser; it performs no data change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": sorted(_NAV_TARGETS)},
                "view": {"type": "string", "description": "for type=navigate: " + "|".join(_VIEWS)},
                "lead_id": {"type": "string", "description": "for type=open_lead"},
                "stage": {"type": "string", "description": "for type=filter_board (a stage name)"},
            },
            "required": ["type"],
        },
    },
]


class AssistantService:
    def __init__(self, db: AsyncSession, user: User, client: LLMClient | None = None):
        self.db = db
        self.user = user
        self.client = client or get_llm_client()
        self._directive: dict | None = None
        self._used: list[str] = []

    async def run(
        self, message: str, history: list[dict] | None = None, context: dict | None = None
    ) -> dict:
        system = self._system_prompt(context)
        messages = self._seed_messages(history, message)
        reply = ""
        for _ in range(MAX_TOOL_TURNS):
            resp = await self.client.complete_with_tools(
                system=system, messages=messages, tools=TOOLS,
                model=settings.LLM_NEGOTIATION_MODEL, max_tokens=settings.LLM_MAX_TOKENS,
            )
            reply = (resp.get("text") or "").strip()
            calls = resp.get("tool_calls") or []
            if not calls:
                break
            messages.append({"role": "assistant", "content": resp.get("content") or []})
            results = []
            for call in calls:
                name = call.get("name", "")
                self._used.append(name)
                try:
                    out = await self._dispatch(name, call.get("input") or {})
                except Exception as exc:  # never let a tool error crash the turn
                    log.warning("assistant_tool_failed", tool=name, error=str(exc))
                    out = {"error": "tool failed"}
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.get("id"),
                    "content": json.dumps(out, default=str)[:TOOL_RESULT_CAP],
                })
            messages.append({"role": "user", "content": results})
        if not reply:
            reply = "Sorry — I couldn't work that out. Try rephrasing?"
        return {"reply": reply, "directive": self._directive, "used_tools": self._used}

    # -- prompt / message assembly --------------------------------------------
    def _system_prompt(self, context: dict | None) -> str:
        role = getattr(self.user.role, "value", str(self.user.role))
        view = (context or {}).get("view") or "the dashboard"
        return (
            "You are the voice co-pilot for the Jane Aerospace sales & onboarding dashboard. "
            f"You are assisting {self.user.full_name} (role: {role}). "
            "You are STRICTLY READ-ONLY: you can look things up and move the dashboard to the right "
            "place, but you must NEVER claim to send an email, approve a document, or change anything — "
            "you have no ability to do so. Always use a tool to fetch real data before stating numbers "
            "or status. Keep answers short and natural (1-3 sentences) because they are read aloud. "
            "Reply in plain spoken text only — no markdown, asterisks, bullet symbols, headings, or emoji. "
            "When the user asks to see, open, or go to something, call the navigate tool with the right "
            f"target. The user is currently on '{view}'. Views for navigate: {', '.join(_VIEWS)}."
        )

    def _seed_messages(self, history: list[dict] | None, message: str) -> list[dict]:
        msgs: list[dict] = []
        for h in (history or [])[-8:]:
            role = h.get("role")
            content = h.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                msgs.append({"role": role, "content": content})
        msgs.append({"role": "user", "content": message})
        return msgs

    # -- tool dispatch --------------------------------------------------------
    async def _dispatch(self, name: str, args: dict) -> dict:
        if name == "navigate":
            return self._navigate(args)
        if name == "search_leads":
            return await self._search_leads(args.get("query", ""))
        if name == "pipeline_summary":
            return await self._pipeline_summary()
        if name == "list_pending":
            return await self._list_pending(args.get("kind", "approvals"))
        if name == "lead_detail":
            return await self._lead_detail(args.get("name_or_id", ""))
        return {"error": f"unknown tool {name}"}

    def _navigate(self, args: dict) -> dict:
        t = args.get("type")
        if t not in _NAV_TARGETS:
            return {"ok": False, "error": "unknown navigation target"}
        if t == "navigate" and args.get("view") not in _VIEWS:
            return {"ok": False, "error": "unknown view"}
        self._directive = {k: v for k, v in args.items() if v is not None}
        return {"ok": True, "navigated": self._directive}

    async def _search_leads(self, query: str) -> dict:
        from app.api.v1.dashboard_endpoints import _lead_row
        q = (query or "").strip().lower()
        leads = (await self.db.execute(select(LeadV2))).scalars().all()
        recs = {r.lead_id: r for r in (await self.db.execute(select(OnboardingRecord))).scalars().all()}
        out = []
        for lead in leads:
            hay = " ".join(filter(None, [lead.business_name, lead.email, lead.contact_name])).lower()
            if q and q not in hay:
                continue
            row = _lead_row(lead, recs.get(lead.id))
            out.append({
                "lead_id": row["lead_id"], "company": row["company"], "email": row["email"],
                "stage": row["stage"], "status": row["status"],
                "booked_at": row["booked_at"], "selected_slot": row["selected_slot"],
            })
            if len(out) >= 12:
                break
        return {"count": len(out), "leads": out}

    async def _pipeline_summary(self) -> dict:
        from app.api.v1.dashboard_endpoints import STAGES, _approval_items, _stage_index
        leads = (await self.db.execute(select(LeadV2))).scalars().all()
        recs = (await self.db.execute(select(OnboardingRecord))).scalars().all()
        rec_by_lead = {r.lead_id: r for r in recs}
        by_stage: dict[str, int] = {}
        for lead in leads:
            s = STAGES[_stage_index(lead, rec_by_lead.get(lead.id))]
            by_stage[s] = by_stage.get(s, 0) + 1
        lead_by_id = {lead.id: lead for lead in leads}
        pending = 0
        for r in recs:
            lead = lead_by_id.get(r.lead_id)
            if lead:
                action, _waiting = _approval_items(r, lead)
                pending += len(action)
        return {
            "total_leads": len(leads),
            "booked": sum(1 for lead in leads if lead.booked_at),
            "onboarding": len(recs),
            "pending_approvals": pending,
            "by_stage": by_stage,
        }

    async def _list_pending(self, kind: str) -> dict:
        from app.api.v1.dashboard_endpoints import _approval_items, _doc_json
        recs = (await self.db.execute(select(OnboardingRecord))).scalars().all()
        if kind == "comments":
            out = []
            for r in recs:
                lead = await self.db.get(LeadV2, r.lead_id)
                if not lead:
                    continue
                for doc_type in ("nda", "agreement"):
                    for c in reversed(_doc_json(r, doc_type).get("comments") or []):
                        if isinstance(c, dict) and not c.get("done"):
                            out.append({
                                "company": lead.business_name or lead.email, "doc": doc_type,
                                "by": c.get("by") or "Lead", "text": (c.get("text") or "")[:300],
                                "at": c.get("at") or "",
                            })
            return {"kind": "comments", "count": len(out), "items": out[:20]}
        items = []
        for r in recs:
            lead = await self.db.get(LeadV2, r.lead_id)
            if not lead:
                continue
            action, _waiting = _approval_items(r, lead)
            for a in action:
                items.append({"company": a.get("company"), "label": a.get("label"),
                              "since": a.get("since"), "email": a.get("email")})
        return {"kind": "approvals", "count": len(items), "items": items[:20]}

    async def _lead_detail(self, key: str) -> dict:
        from app.api.v1.dashboard_endpoints import _lead_row
        key = (key or "").strip()
        lead = None
        try:
            lead = await self.db.get(LeadV2, uuid.UUID(key))
        except (ValueError, TypeError):
            lead = None
        if lead is None:
            kl = key.lower()
            for cand in (await self.db.execute(select(LeadV2))).scalars().all():
                if kl and (kl in (cand.business_name or "").lower() or kl in (cand.email or "").lower()):
                    lead = cand
                    break
        if lead is None:
            return {"found": False}
        rec = (await self.db.execute(
            select(OnboardingRecord).where(OnboardingRecord.lead_id == lead.id)
        )).scalar_one_or_none()
        return {"found": True, "lead": _lead_row(lead, rec)}
