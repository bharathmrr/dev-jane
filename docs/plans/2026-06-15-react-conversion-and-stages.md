# Jane Aerospace Dashboard — Conversion & Feature Plan

**Date:** 2026-06-15
**Order (user-chosen):** Stage 1 (React) → 2 (Booking) → 3 (Review/Signing) → 4 (Sheet↔CSV) → Later (test script)
**Commit-first:** declined by user — proceeding without committing (work-loss risk acknowledged).

---

## Stage 1 — Convert the dashboard to React (same functionality, richer UI)

**Goal:** Replace the vanilla-JS `app/templates/dashboard.html` with a React app that has identical
functionality and a richer UI (gradient KPI tiles, score rings, polished layout), reusing the existing
`/api/v1/dashboard/api/*` REST API unchanged. The signer/editor pages (`/editor`, `/sign`) stay as-is
for now; only the operator dashboard is converted in Stage 1.

**Stack & serving**
- `frontend/` — Vite + React + TypeScript + Tailwind CSS (rich, fast, standard).
- Build output (`frontend/dist`) served by FastAPI at the existing dashboard route; `index.html` returned
  by the dashboard page handler, hashed assets under a static mount. **No API changes.**
- The current vanilla-JS dashboard stays live until the React build reaches parity, so nothing breaks mid-migration.

**Sub-phases (each independently verifiable):**
1. **1.1 Scaffold** — create the Vite app, Tailwind, an `api` client (token in localStorage, same endpoints),
   a build script, and FastAPI wiring to serve `dist/`. Verify: build succeeds, login works against the real API.
2. **1.2 Shell + auth** — login screen, sidebar nav, topbar, user menu, route switching, notifications bell,
   comments + activity drawers. Verify each opens and calls the right endpoint.
3. **1.3 Overview** — gradient KPI tiles, conversion funnel, charts (Chart.js/Recharts), integrations.
4. **1.4 Pipeline Board** — column-based cards (the lifecycle board), drag/menu actions.
5. **1.5 Leads** — table, detail drawer, lead actions, search.
6. **1.6 Sheet** — editable Google-Sheet grid + save.
7. **1.7 Bookings** — calendar + day-block.
8. **1.8 Documents** — the documents table (status chips, Edit/Sign links).
9. **1.9 Notifications / Approvals + Users** — approval actions, user management.
10. **1.10 Cutover** — point the dashboard route at the React build; keep the old file as a fallback for one release.

**Risk:** largest, highest-risk piece — done view-by-view with the old UI as the safety net. Requires Node/npm (Node confirmed present; npm to be verified in 1.1).

---

## Stage 2 — Booking: cancel/rebook by reply + slot availability

1. **Reply-driven cancel/reschedule** — when a lead's email reply intent is "cancel" or "reschedule"
   (already classified by the LLM intent service), actually act on the booking: cancel it, or offer/confirm
   a new slot. Wire the intent → booking state transition (cancel / re-offer) in the reply processor.
2. **Slot availability cleanup** — fix availability showing unnecessary/invalid slots (e.g., past slots,
   already-booked, outside working hours, duplicates). Audit the slot generator + Zoho availability and filter.

Verify: simulate cancel/reschedule replies; confirm booking state changes and no invalid slots are offered.

---

## Stage 3 — Editor review tracking + signing flow

1. **Review tracker ("circles"/stepper)** — a visible progress tracker on the editor + documents views:
   Draft → Sent for Review → T&C Accepted → Internal Sign → Lead Sign → Signed. Driven by the existing
   document `stage`/status fields so you can see exactly where each doc is.
2. **In-app review email options** — "Send for review" actions (send queries / send link) that submit
   **in-app** (no jump to another panel), with success feedback.
3. **Lead signing editor** — the lead's View/Sign link opens **our own editor** (not an external app),
   with the full-screen placement, **multiple-signature tracking**, and **drag & select** placement.
   Backend foundation already in (preview token accepts sign-token; `/sign` accepts placed overlays).
   Remaining: the signer-facing placement UI on the `/sign` page + the review-stage flow back into the editor.

Verify: walk a doc through review → T&C accept → sign; confirm tracker updates and the lead places a signature.

---

## Stage 4 — Sheet ↔ CSV two-way sync

- Today: adding a row in the Google Sheet does not update the CSV (one-way). Make it **two-way**:
  Sheet edits propagate to the CSV (and DB), and CSV/DB changes reflect in the Sheet. Reconcile by a stable
  key (email), no duplicates. Email formatting unchanged.

Verify: add/edit a row in the Sheet → CSV updates; add via CSV/dashboard → Sheet updates.

---

## Later — booking concurrency test script

- Standalone script (no DB writes, no email): simulate **~10 different people booking concurrently** against
  the slot logic and assert **no two get the same slot** (uniqueness/locking test). Pure test harness, run manually.

---

## Cross-cutting
- After each stage: run the existing checks (py_compile, JS/parse checks, behavioral tests) and confirm nothing regressed.
- Keep the working app live throughout; never replace a working surface until its replacement is at parity.
