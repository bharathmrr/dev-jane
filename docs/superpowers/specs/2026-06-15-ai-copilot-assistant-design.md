# AI Co-pilot Assistant — Design Spec

**Date:** 2026-06-15
**Status:** Approved for planning
**Scope:** v1 of an in-dashboard AI assistant for the Jane Aerospace lead/onboarding platform.

---

## 1. Summary

A **voice-first read-only co-pilot** in the dashboard SPA (`app/templates/dashboard.html`), driven by a **single floating mic button — no chat widget, no typed input, no transcript window**. The operator presses the button and speaks; the assistant **answers out loud** and **navigates** the dashboard to the right place. It greets the user by name on login. A small transient caption shows what it heard and its reply, then auto-fades (not a persistent chat).

It **never** performs an outward or state-changing operation — no sending email, no approvals, no lead edits. Those clicks remain the human's. This is the defining safety property: **no write tool exists in the agent at all**, so even a malicious or confused prompt cannot act.

Voice is **browser-native and free** (Web Speech API): speech-in (STT) and speech-out (TTS) with multi-voice / multi-language support, no API key, no per-use cost.

---

## 2. Goals & non-goals

### Goals (v1)
- Personalized greeting on login: **"Welcome back, {name}"**.
- Conversational Q&A and summaries over the operator's pipeline/onboarding data.
- Find a lead/document and **navigate the dashboard** to it ("open Acme's NDA", "show approvals waiting > 3 days", "take me to this week's bookings").
- Text chat **and** voice (talk to it, it talks back), free and keyless.
- Works **with no Anthropic API key** at a basic level (stub fallback).

### Non-goals (explicitly out of scope for v1 — YAGNI)
- No write/actions: no send, approve, drop, edit, start-onboarding from the agent.
- No Hugging Face / Whisper / server-side audio / paid voice APIs.
- **No chat widget** — no message list, no typed input box, no persistent transcript. Voice-first only.
- No cross-session persisted history (a short in-memory turn buffer per browser session for follow-ups only).
- No long-term memory beyond the current conversation window.
- No customer-facing bot (the agent is internal/operator-only).

---

## 3. Architecture

```
Browser (dashboard.html)                     Backend (FastAPI)
┌─────────────────────────────┐              ┌────────────────────────────────┐
│ Chat widget (🤖)            │  POST /api/  │ dashboard_endpoints.py          │
│  • text input + mic (STT)   │  assistant   │  assistant_chat()  [auth=user]  │
│  • voice on/off + picker    │ ───────────▶ │        │                        │
│  • render reply (TTS out)   │              │        ▼                        │
│  • execute UI directive     │ ◀─────────── │ services/assistant.py           │
└─────────────────────────────┘ {reply,      │  AssistantService.run()         │
                                 directive?}  │   Anthropic tool-use loop       │
                                              │   READ tools ⇄ DB (scoped)      │
                                              │   llm_client (anthropic|stub)   │
                                              └────────────────────────────────┘
```

### Components

**Backend**
- `app/services/assistant.py` — new `AssistantService`. Runs the tool-use loop, owns the system prompt (includes current user name + role), the read-only tool implementations, and assembly of the final `{reply, directive}`.
- `app/api/v1/dashboard_endpoints.py` — new endpoint `POST /dashboard/api/assistant` (full path `/api/v1/dashboard/api/assistant`), `Depends(get_current_user)`. Body `{message: str, history: list[{role,content}] = [], context: dict | None}`. Returns `{reply: str, directive: dict | None, used_tools: list[str]}`.
- `app/services/llm_client.py` — extend with a tool-calling method (Anthropic Messages API with `tools=`), e.g. `complete_with_tools(system, messages, tools, model, max_tokens) -> {text, tool_calls}`. Keep `StubClient` working: stub returns a canned reply and does simple keyword → directive routing so the feature degrades gracefully with no key.

**Frontend** (`app/templates/dashboard.html`, vanilla JS) — **voice button only, no chat widget**
- A single floating 🎤 mic button (bottom-right) with visible states: **idle → listening (pulse) → thinking (spinner) → speaking**.
- Press → `SpeechRecognition` captures one utterance → POST to `/api/assistant` → speak the reply (TTS) + execute any navigation directive.
- **Transient caption** above the button shows the recognized request and the reply, then auto-fades after a few seconds. No message list, no text input, no history.
- TTS: `speechSynthesis.speak(new SpeechSynthesisUtterance(reply))` using the selected voice. A tiny settings popover (long-press / gear) holds the **voice picker** from `speechSynthesis.getVoices()` (multi-voice / multi-language) and a mute toggle.
- Directive executor: maps `directive.type` to existing dashboard JS (`nav()`, `openDrawer()`, `openComments()`, `toggleNotifications()`, board filter).
- Greeting: on first dashboard load after login, **speak** "Welcome back, {full_name}" and show it as a caption.
- Degradation: if `SpeechRecognition` is unsupported (e.g. Firefox), the mic button falls back to a one-line browser `prompt()` for the request (still no persistent chat); TTS replies continue to work.

---

## 4. The agent's tools (all READ-ONLY)

Server-side tools query the DB scoped to what the user's role can already see (reuse existing helpers `_lead_row`, `_approval_items`, `_doc_json`, `comments_feed` logic):

| Tool | Purpose |
|------|---------|
| `search_leads(query)` | Find leads by company/email/contact; return stage, status, booked time. |
| `pipeline_summary()` | Totals by stage, conversion, counts (mirrors `/overview`). |
| `list_pending(kind)` | `approvals` (action-required), `comments` (open), `stuck` (docs idle > 3 days). |
| `lead_detail(name_or_id)` | Full onboarding status for one lead (KYC/NDA/Agreement, follow-ups). |

**Navigation directives** are not server actions — the tool's "result" is simply the directive echoed back to the browser, which executes it via existing JS:

| Directive | Browser action |
|-----------|----------------|
| `{type:"navigate", view}` | `nav(view)` |
| `{type:"open_lead", lead_id}` | `openDrawer(lead_id)` |
| `{type:"open_comments"}` | `openComments()` |
| `{type:"open_notifications"}` | `toggleNotifications()` |
| `{type:"filter_board", stage}` | render board filtered to a stage |

The agent returns **at most one** directive per turn alongside its text reply.

---

## 5. Data flow (one turn)

1. User types, or speaks → `SpeechRecognition` transcribes to text.
2. Browser POSTs `{message, history (trimmed to last ~8 turns), context:{view}}` to `/api/v1/dashboard/api/assistant` with the auth token.
3. `AssistantService.run()` builds the system prompt (role, name, tool catalog, "read-only, never act"), then loops the Anthropic tool-use cycle: model requests a read tool → service runs it against the DB → feeds the result back → until the model returns final text (+ optional navigation directive).
4. Endpoint returns `{reply, directive?, used_tools}`.
5. Browser renders the reply, speaks it if voice-out is on, and executes any directive.

---

## 6. Personalization

- Login already returns `full_name`, `email`, `role` (`dashboard_login`). The dashboard stores these and shows **"Welcome back, {full_name}"** (chat seed + a brief top-bar toast on first load).
- The assistant endpoint resolves the current `User` via `get_current_user`; its system prompt includes the user's **name, role, and id**, so replies are personalized and naturally scoped. No new identity plumbing required.

---

## 7. Error handling & degradation

- **No Anthropic key** → `StubClient`: answers basic queries and keyword-routes navigation ("show approvals" → `navigate('approvals')`). Feature stays usable.
- **LLM / network failure** → endpoint returns a friendly error; chat panel stays open and usable, no crash.
- **Tool error / no data** → caught server-side; the model is told "no results," replies cleanly.
- **Mic unsupported (e.g. Firefox) or permission denied** → hide/disable mic, keep text input and spoken replies. No console errors surfaced to the user.
- **Role scoping** → tools never return data the user couldn't already load through the normal dashboard endpoints.

---

## 8. Success criteria

- "What's pending approval?" → correct summary; optionally jumps to Notifications.
- "Open Acme's NDA" → that lead/document opens in the dashboard.
- "How many leads booked this week?" → correct count.
- Spoken question (Chrome/Edge) → transcribed, answered, and spoken back with the chosen voice.
- With no API key, the assistant still answers basics and navigates.
- The agent has **no code path** that can send, approve, or modify anything.

---

## 9. Build slices (for the implementation plan)

1. **Backend brain** — `llm_client.complete_with_tools` + `AssistantService` (tools + system prompt) + endpoint. Unit-test the tool loop with the stub provider.
2. **Voice button UI** — floating mic button with idle/listening/thinking/speaking states, STT capture, POST wiring, directive executor, transient caption, welcome-back greeting (spoken).
3. **Voice output + picker** — TTS playback, settings popover with multi-voice picker + mute, graceful degradation when STT is unsupported.
4. **Polish** — error/empty handling, history trimming, responsive button placement.
