# Server Infrastructure — Full Specification Document
## Jane Aerospace AI Platform — 3 Systems

---

## Part 1 — Why You Need a Server At All

The platform cannot run on a laptop, a shared hosting plan, or a basic web host. Here is exactly why.

**The system never sleeps.**
All three AI systems run background tasks 24 hours a day, 7 days a week. The Customer Onboarding system scans the inbox every 20 seconds and processes every reply within minutes — whether the lead writes at 2 AM or on a Sunday. The Vendor Onboarding system polls Zoho Sign every 2 hours, day and night, waiting for NDA and agreement signatures. The Supply Chain system will check shipment tracking and payment status on its own schedule. These are not jobs you trigger manually. They run because a dedicated process is always alive, always connected to the internet, always waiting. That only exists on a server.

**Webhooks need a permanent address.**
Zoho CRM, Zoho Contracts, Zoho Bookings, KARZA, and GLEIF all push data to your system in real time. They require a permanent HTTPS URL — for example `https://procurement.janeaerospace.co.in/webhook/zoho`. That address is your server. If the server is off, or if you are using a temporary tool like ngrok (which expires), Zoho cannot reach you and you miss events — a lead books a meeting, it never registers; an NDA is signed, the pipeline does not advance.

**The database cannot be on a laptop.**
Every lead, every onboarding record, every KYC submission, every NDA contract, every audit log entry — these must be stored in one place, always accessible, never lost. Whether you use a managed cloud database (Neon) or a self-hosted PostgreSQL, it must run continuously and be reachable from the server at all times.

**AI processing is constant.**
Claude API calls run on every inbound email reply to classify intent and generate a response. KYC verification calls hit KARZA and GLEIF on every form submission. Celery workers execute all of this in the background so no user ever waits. These tasks run in parallel — one lead being processed never blocks another. That concurrency requires a machine with dedicated resources: CPU, RAM, and fast disk.

**Multiple employees access this simultaneously.**
Your team receives email notifications with approve/reject links. Multiple team members may click different approval links at the same moment. The server handles all of these concurrently without conflict.

---

## Part 2 — Why Two Servers (Testing + Production)

You never test code on a machine that real customers depend on. This is non-negotiable. Here is what goes wrong with only one server.

| Scenario | Single Server Result | Two Server Result |
|----------|---------------------|------------------|
| Developer pushes a bug in onboarding flow | Real customer KYC links break | Bug caught on test server, customers unaffected |
| New Claude prompt generates wrong email | Wrong email sent to real lead | Tested on dummy data first, then deployed |
| NDA template change corrupts existing contracts | Real contracts broken | Caught in testing before production |
| Server rebooted for OS update | System offline, Zoho webhooks miss events | Test server updated first; production zero-downtime |
| KARZA integration update breaks KYC | Real onboarding fails silently | Caught in sandbox before going live |
| Celery Beat schedule changed incorrectly | Emails sent at wrong times | Validated on test data first |

### The Two-Server Model

| | Testing Server | Production Server |
|--|---------------|-----------------|
| Purpose | Development, QA, new features | Live 24/7 customer operations |
| Email accounts | Dummy Gmail accounts | Real Zoho Mail / Gmail |
| APIs | KARZA sandbox, Zoho sandbox | Real KARZA, real Zoho, real GLEIF |
| Database | Test data only | Real customer and onboarding data |
| Can be rebooted | Anytime | Only with a plan and zero-downtime strategy |
| Claude API | Same API — test prompts | Production-validated prompts |
| Who uses it | Developers and QA | Leads, vendors, your team |
| Spec | Smaller — cost-efficient | Full spec — reliability-first |

---

## Part 3 — What Runs on Each Server (In Detail)

### 1. Nginx (Reverse Proxy + SSL)

**What it does:** Sits at the front door of the server. Every request from the internet — Zoho webhook calls, lead clicking a booking slot link, team member clicking an approval email — hits Nginx first. It handles HTTPS encryption, routes each request to the right internal service (FastAPI), applies rate limiting, and blocks malicious traffic.

**Why it is needed:** Without Nginx, your system has no SSL certificate. Zoho rejects HTTP webhooks — it only sends to HTTPS URLs. Without Nginx, anyone on the internet can spam your endpoints. Without Nginx, there is no clean separation between public traffic and internal services.

**Resources used:** Minimal — 256 MB RAM, 1 CPU core shared with other services.

---

### 2. FastAPI Application

**What it does:** The entry point for all external events. It receives Zoho CRM webhook calls (when a new lead is created), Zoho Bookings webhook calls (meeting confirmed, cancelled, no-show), Zoho Contracts webhook calls (NDA signed, agreement signed), KYC form submissions from leads and vendors, approval button clicks from team members in email notifications, slot selection clicks from leads, and open-tracking pixel loads. FastAPI does not do heavy work itself — it hands everything off to Celery immediately and returns a fast response.

**Why it is needed:** Every automated action in all three systems starts when something calls FastAPI. If FastAPI is not running, nothing works — no webhooks are received, no forms can be submitted, no approval links function.

**Resources used:** 2–4 worker processes × 256 MB = 512 MB to 1 GB RAM, 2–4 CPU cores under load.

---

### 3. Celery Workers (Task Queue Executor)

**What it does:** Executes all background work for all three systems in parallel. For Customer Onboarding: polls Gmail IMAP every 20 seconds, classifies reply intent with Claude, generates AI email responses, sends slot emails, processes bookings, handles bounces, no-shows, follow-ups, and A/B test tracking. For Vendor Onboarding: sends KYC form emails, calls KARZA for Indian company verification, calls GLEIF for overseas LEI verification, creates contracts via Zoho Contracts API, polls Zoho Sign for signature status, sends approval notification emails to the team, advances the pipeline stage automatically. Multiple workers run simultaneously, meaning one vendor's KYC verification never blocks another lead's slot booking.

**Why it is needed:** Without Celery, FastAPI would process every task synchronously — one at a time, blocking. A single KARZA API call (which takes 2–3 seconds) would freeze the entire system for that duration. With Celery, 4 to 8 tasks run simultaneously in parallel across different queues.

**Resources used:** 4–8 concurrent workers × 512 MB = 2–4 GB RAM, 4–8 CPU cores. This is the most CPU-intensive component.

---

### 4. Celery Beat (Scheduler)

**What it does:** The system's internal clock. Triggers every periodic task automatically: IMAP inbox polling every 20 seconds, pipeline sheet export every 60 seconds, slot booking nudge checks every 5 minutes, OOO lead resumption daily at 07:30 UTC, no-show detection every 30 minutes, scheduled follow-up dispatch every hour, soft-bounce retry every 6 hours, Zoho Sign signature polling every 2 hours. Exactly one Beat process must run at all times — if it stops, all periodic automation stops with it.

**Why it is needed:** Without Celery Beat, none of the automation is periodic. The inbox is never polled. Follow-up emails never send. NDA signatures are never detected. The entire hands-free design of all three systems depends on Beat running continuously.

**Resources used:** Minimal — 256 MB RAM, essentially no CPU.

---

### 5. PostgreSQL (Primary Database)

**What it does:** Stores the permanent record of everything across all three systems. For Customer Onboarding: leads, bookings, slot reservations, IMAP email message IDs (for deduplication), A/B test counters, audit logs, organizer availability. For Vendor Onboarding: onboarding records, KYC submissions, company verification results, NDA contract IDs, agreement contract IDs, pipeline stages, team approval history. For Supply Chain (future): deals, suppliers, documents, cost calculations, tracking events, payment records. This is the single source of truth for the entire business.

**Why it is needed:** Every action in every system reads from or writes to PostgreSQL. The slot reservation system uses a database-level unique index as the final guard against double-booking. The onboarding pipeline state machine writes every transition here. If this goes down, the system has no memory of any deal, lead, or vendor.

**Current setup:** Neon (managed cloud PostgreSQL) — no physical server required. Neon handles backups, scaling, and high availability automatically.

**Resources used (if self-hosted):** 8–16 GB RAM dedicated, 1–4 CPU cores, NVMe SSD mandatory for fast I/O.

---

### 6. Redis (Message Broker + Cache)

**What it does:** Two distinct jobs. First — it is the pipe between FastAPI and Celery. When FastAPI receives a Zoho webhook, it writes a task message to Redis, and Celery picks it up within milliseconds. Without this pipe, Celery cannot receive work. Second — it holds all short-lived state: slot reservation locks (so two leads cannot book the same meeting slot simultaneously), IMAP deduplication keys (so the same email is never processed twice), SMTP daily send counter (capped at 400 emails per day), approval token expiry, and A/B test counters.

**Why it is needed:** Without Redis, Celery has no broker and cannot function. The slot locking system that prevents double-booking collapses. The email deduplication system that prevents the same reply from being processed twice collapses.

**Current setup:** Upstash or Redis Cloud (managed, free tier) — no physical server required for current scale.

**Resources used (if self-hosted):** 1–4 GB RAM (all data lives in memory — that is why Redis is fast), minimal CPU.

---

### 7. Weaviate (Vector Database — Knowledge Base)

**What it does:** Stores AI embeddings of historical emails, customs query responses, supplier histories, and product data as numerical vectors. When the Supply Chain AI agent needs to answer a customs query, Weaviate finds the most similar past queries and responses from Jane Aerospace's own history in milliseconds — not from generic internet knowledge, but from your specific 3 years of operational history. This is what makes AI answers accurate and contextual rather than generic.

**Why it is needed:** This is the competitive advantage of the Supply Chain system. Without Weaviate, Claude answers from general knowledge. With Weaviate, Claude answers from Jane Aerospace's exact past cases. A 200,000-email vector index at 1536 dimensions requires 30–40 GB of RAM to serve fast searches.

**When needed:** Not required for Customer Onboarding or Vendor Onboarding systems. Required when Supply Chain AI is built.

**Resources used:** 30–40 GB RAM, 200–300 GB NVMe SSD for persistent vector storage.

---

### 8. Playwright Service (Headless Browser Pool)

**What it does:** A pool of invisible Chrome browsers the Supply Chain system uses to scrape supplier websites, e-commerce portals (IndiaMART, Alibaba, Amazon Business), and freight rate pages for price verification. Most supplier portals render their content with JavaScript — a plain HTTP request cannot read these pages. Only a real browser can. Playwright runs Chrome with no visible window.

**When needed:** Required only for the Supply Chain system. Not needed for Customer Onboarding or Vendor Onboarding.

**Resources used:** Each browser instance uses 300–500 MB RAM. With 5 concurrent sessions: approximately 2.5 GB RAM, 2–4 CPU cores.

---

### 9. MinIO (Document Storage)

**What it does:** An S3-compatible file storage system running on your own server. For the Vendor Onboarding system, it stores uploaded KYC documents (GST certificates, incorporation certificates, PAN cards). For the Supply Chain system, it will store BOE documents, SWIFT copies, proforma invoices, packing lists, and tax invoices. Every file is stored by deal or onboarding reference ID with versioning — if a document is re-uploaded, the old version is preserved.

**Why it is needed:** Files cannot be stored as raw paths on a disk with no management system. MinIO provides versioning, a clean API to retrieve any document by ID, and bucket-based organisation by document type.

**When needed:** Useful now for KYC document storage. Essential when Supply Chain system is built.

**Resources used:** 2–4 GB RAM, minimal CPU, high disk usage — 500 GB to 2 TB over 3 years.

---

### 10. ELK Stack — Elasticsearch + Logstash + Kibana (Logging)

**What it does:** Collects all structured JSON logs from every task, every API call, every AI response across all three systems. Kibana provides a web dashboard where you can type any lead email, onboarding ID, or deal reference and see its complete timeline — every action, every error, every state change in chronological order.

**Why it is needed:** When something goes wrong — a lead says they never received the KYC form, a vendor says their NDA link expired, a deal is stuck — you need to see exactly what happened and when. Without centralised logging, you have no visibility. With ELK, the full story appears in 3 seconds.

**Resources used:** 8–16 GB RAM, 2–4 CPU cores, 200 GB SSD for log storage.

---

### 11. Prometheus + Grafana (System Health Monitoring)

**What it does:** Prometheus collects numerical metrics every 15 seconds — CPU usage, RAM usage, Celery queue depth per queue, task success and failure rates, FastAPI request latency, PostgreSQL query times. Grafana displays these as live dashboards with configurable alerts — for example, if the Celery queue depth exceeds 50 tasks, or if the IMAP polling task stops running, the team is notified immediately.

**Why it is needed:** You have real customers and vendors depending on this system. If a Celery worker crashes silently at 3 AM, you need to know within 1 minute — not when a customer complains hours later. This is already provisioned in the existing Docker Compose configuration.

**Resources used:** 2–4 GB RAM combined, minimal CPU.

---

## Part 4 — Server Specifications

### Testing Server

**Purpose:** Development, feature validation, QA testing, new AI system development, testing new Claude prompts, testing Zoho integrations with sandbox credentials.

| Component | Specification | Reason |
|-----------|--------------|--------|
| CPU | 8 cores / 16 threads (Intel Xeon or AMD EPYC entry) | Celery workers + FastAPI + all services |
| RAM | 32 GB DDR4 ECC | PostgreSQL (4 GB) + Redis (2 GB) + Celery (4 GB) + FastAPI + OS headroom |
| Primary Storage | 2× 512 GB NVMe SSD in RAID 1 | OS + all applications + database — NVMe for speed, RAID 1 for mirror |
| Secondary Storage | 1 TB SATA SSD | Test document uploads, logs |
| Network | 1 Gbps NIC | All outbound API calls to Zoho, Claude, KARZA |
| Power Supply | Single PSU | Needs to stay on during development |
| Remote Management | iDRAC / iLO | Developers can restart services remotely |
| OS | Ubuntu 22.04 LTS | Docker-native, stable |

---

### Production Server

**Purpose:** Live 24/7 operation — real leads, real vendors, real KYC, real contracts, real bookings. This server cannot go down.

| Component | Specification | Reason |
|-----------|--------------|--------|
| CPU | 2× 16-core Intel Xeon Gold or AMD EPYC (32 cores / 64 threads total) | Celery workers (8 concurrent) + FastAPI (4 workers) + Playwright (Supply Chain) + Weaviate search — all compete for CPU at peak |
| RAM | 128 GB DDR4 ECC (expandable to 512 GB) | PostgreSQL (16 GB) + Redis (4 GB) + Weaviate (40 GB when built) + Playwright (3 GB) + ELK (16 GB) + FastAPI + Celery + Grafana + OS + headroom for Supply Chain AI |
| Primary Storage | 4× 1 TB NVMe PCIe Gen4 SSD in RAID 10 | OS + PostgreSQL (if self-hosted) + Redis + all apps. RAID 10 = speed + redundancy. One NVMe can fail with no interruption |
| Secondary Storage | 4× 2 TB SATA SSD in RAID 10 | KYC documents + future deal documents + Weaviate persistent storage + Elasticsearch logs |
| Backup Storage | 2× 4 TB HDD | Weekly full backup + daily incremental. Physically separate from primary |
| Network | 2× 10 Gbps NIC (bonded) | Bonded pair — if one NIC fails the other continues. All API traffic goes out this interface |
| Power Supply | 2× 800 W Hot-Swap Redundant PSU | If one PSU fails at 3 AM, the second takes over. System never loses power |
| UPS | APC Smart-UPS 2200 VA or equivalent | Protects against power outages — gives 15–30 minutes to shut down gracefully |
| Remote Management | iDRAC Enterprise (Dell) or iLO Advanced (HP) | Full KVM-over-IP console, power cycle, hardware health monitoring without physical access |
| RAID Controller | Hardware RAID controller with battery-backed cache | Software RAID burdens the main CPU. Hardware controller has its own processor |
| OS | Ubuntu 22.04 LTS Server | Stable, 10-year LTS, Docker-native |
| Cooling | Rack-mounted 1U or 2U with hot-swap fans | Fans can be replaced without shutdown |

---

## Part 5 — What Runs Where (Side-by-Side)

| Service | Testing Server | Production Server | Notes |
|---------|--------------|-----------------|-------|
| Nginx | HTTP only (no real SSL needed) | HTTPS via Let's Encrypt | Production SSL required for Zoho webhooks |
| FastAPI | 2 workers | 4 workers | Handles all webhooks and form submissions |
| Celery Workers | 4 concurrent | 8 concurrent | Email, onboarding, AI agent, scheduler queues |
| Celery Beat | 1 instance | 1 instance | Never run more than one Beat |
| PostgreSQL | Test data only (or Neon sandbox) | Real data / Neon production | All leads, onboarding records, bookings |
| Redis | Local Docker or Upstash sandbox | Upstash / Redis Cloud production | Broker + slot locks + dedup |
| Weaviate | Small index (test emails only) | Full index (when Supply Chain built) | Not needed until System 3 |
| Playwright | 2 concurrent | 5 concurrent | Only when Supply Chain system is built |
| MinIO | Test KYC documents | Real KYC + deal documents (encrypted) | Can use cloud S3 alternative |
| ELK Stack | Smaller index, shorter retention | Full retention (3 years) | Optional now, essential at scale |
| Prometheus + Grafana | Both | Both | Already provisioned in Docker Compose |
| Zoho Mail | Sandbox / test account | Real Zoho Mail | |
| Claude API | Same API — test prompts | Production-validated prompts | Claude bills per token in both |
| KARZA KYC | `testapi.karza.in` sandbox | `api.karza.in` production | Swap one `.env` variable |
| GLEIF LEI | Same API (free, no sandbox) | Same API | No difference |

---

## Part 6 — What to Use Instead of Ngrok

The current system uses ngrok for a temporary public URL (`APP_URL`). Ngrok free tier expires and the URL changes each restart. This breaks all Zoho webhook registrations.

### Recommended Replacement: Cloudflare Tunnel

| | ngrok Free | Cloudflare Tunnel |
|--|-----------|------------------|
| Cost | Free but expires | Free, never expires |
| URL stability | Changes on restart | Permanent |
| Custom domain | Paid plan only | Free with your domain |
| Speed | Adequate | Fast (Cloudflare CDN) |
| SSL | Yes | Yes (automatic) |
| Setup complexity | Low | Low |

Cloudflare Tunnel creates a permanent, encrypted tunnel from `procurement.janeaerospace.co.in` to your server's local FastAPI port. No ports need to be opened on your firewall. The URL never changes.

Once configured, set in `.env`:

```
APP_URL=https://procurement.janeaerospace.co.in
```

All Zoho webhook registrations, KYC form links, slot booking links, approval email links, and NDA signing links become permanent and use your own domain.

---

## Part 7 — Storage Calculation

For current 3 systems over 3 years:

| Data Type | Estimated Size | Where Stored |
|-----------|--------------|-------------|
| PostgreSQL — leads, bookings, onboarding records, audit logs | 50–150 GB | Neon cloud / NVMe RAID 10 |
| KYC documents (GST certificates, incorporation docs, PAN) | 10–50 GB | MinIO / SATA SSD RAID 10 |
| Weaviate vectors — when Supply Chain built (200K emails × 1536 dims) | 200–250 GB | NVMe RAID 10 |
| Deal documents — BOE, SWIFT, PI, invoices (Supply Chain) | 20 GB per 1000 deals | SATA SSD RAID 10 |
| Elasticsearch logs — all 3 systems, 3 years | 100–200 GB | SATA SSD RAID 10 |
| Future AI systems headroom | Reserve 1–2 TB | SATA SSD RAID 10 |
| **Total** | **~2–3 TB active data** | |

---

## Part 8 — Exact Specification Sheet to Hand to Your Server Supplier

```
DOCUMENT: Server Purchase Requirement — Jane Aerospace
Date: June 2026

We require 2 servers with the following specifications.


SERVER 1 — TESTING / DEVELOPMENT

Form Factor      : Rack-mounted 1U or 2U, or Tower
CPU              : 1× Intel Xeon Silver 4310 (12 cores, 24 threads) or
                   AMD EPYC 7252 (8 cores) or equivalent entry-level server CPU
RAM              : 32 GB DDR4 ECC Registered (2× 16 GB DIMMs),
                   expandable to 64 GB (spare slots required)
Primary Storage  : 2× 512 GB NVMe PCIe Gen4 SSD configured in RAID 1
Secondary Storage: 1× 1 TB SATA SSD (no RAID required)
RAID Controller  : Hardware RAID controller supporting RAID 1 on NVMe
Network          : 2× 1 GbE NIC ports (RJ45)
Power Supply     : 1× 600 W PSU
Remote Mgmt      : iDRAC Express (Dell) or iLO Standard (HP) or equivalent
Cooling          : Standard rack fans
OS               : None (we install Ubuntu 22.04 LTS)
Standards        : RoHS compliant


SERVER 2 — PRODUCTION / DEPLOYMENT (Primary Business Server — 24/7)

Form Factor      : Rack-mounted 1U or 2U
CPU              : 2× Intel Xeon Gold 6312U (24 cores each, 48 cores / 96 threads total)
                   OR 2× AMD EPYC 7313 (16 cores each, 32 cores / 64 threads total)
                   — dual-socket motherboard required
RAM              : 128 GB DDR4 ECC Registered (4× 32 GB DIMMs),
                   expandable to 512 GB or 1 TB
                   (spare DIMM slots required — we are adding more AI systems)
Primary Storage  : 4× 1 TB NVMe PCIe Gen4 SSD configured in RAID 10
Secondary Storage: 4× 2 TB SATA SSD configured in RAID 10
Backup Storage   : 2× 4 TB 7200 RPM HDD (internal hot-swap bays or external eSATA)
RAID Controller  : Dedicated hardware RAID controller (LSI MegaRAID 9560 or equivalent)
                   with 4 GB battery-backed write cache
                   — must support NVMe RAID 10 and SATA RAID 10 simultaneously
Network          : 2× 10 GbE NIC (SFP+ or RJ45) — bonded pair for failover
Power Supply     : 2× 800 W Hot-Swap Redundant PSU
Fans             : Hot-swap redundant fans
UPS Compatibility: Must accept APC Smart-UPS 2200 VA or 3000 VA input
Remote Mgmt      : iDRAC Enterprise (Dell) or iLO Advanced (HP)
                   — full KVM-over-IP, hardware health, out-of-band management
OS               : None (we install Ubuntu 22.04 LTS)
Rack Rails       : Include rack-mount rail kit
Temperature Range: 10°C to 35°C (standard server room / data centre)
Warranty         : Minimum 3-year on-site next-business-day hardware warranty
```

---

## Part 9 — Additional Items to Request from Supplier

| Item | Qty | Purpose |
|------|-----|---------|
| APC Smart-UPS 2200 VA or 3000 VA | 1 | Protects production server from power outages |
| 8-port managed Gigabit network switch | 1 | Both servers + employee workstation network |
| Cat6A patch cables (1 m, 2 m) | 6 | Server to switch connections |
| 42U server rack with cable management | 1 (if no rack exists) | Houses both servers and networking gear |
| Rack PDU with remote monitoring | 1 | Managed power distribution in the rack |

---

## Part 10 — Questions to Ask Your Server Supplier

1. Can the production server RAM be expanded to 512 GB or 1 TB in the future without replacing the existing DIMMs — do enough empty slots exist?
2. Is the RAID controller hot-swap capable — can a failed NVMe or SATA SSD be replaced without shutting down the server?
3. What is the MTBF (Mean Time Between Failures) rating for the redundant PSUs?
4. Does iDRAC / iLO remote management require a dedicated network port or does it share the primary NIC?
5. What is the on-site support model — do engineers arrive within 24 hours if a hardware component fails?
6. Are the NVMe slots M.2 or U.2 form factor?
7. What is the maximum power draw under full CPU + disk load — confirm that the UPS rating covers it with 30% headroom?

---

## Part 11 — Why This Scales to All Three Future AI Systems

| System | Additional Load | Handled By |
|--------|---------------|------------|
| Customer Onboarding AI (built) | 8 Celery workers, Claude API calls, Redis locks, IMAP polling | 128 GB RAM has headroom; Neon handles DB |
| Vendor Onboarding AI (built) | Zoho Contracts API, KARZA calls, Zoho Sign polling, PDF generation | Same workers, separate Celery queue; NVMe handles I/O |
| Supply Chain AI (future) | Weaviate (40 GB), Playwright × 5, MinIO, ELK Stack, more workers | RAM expandable to 512 GB; PCIe slots available for GPU |
| Quality Control AI (future) | Image recognition, GPU compute | PCIe x16 slot available for GPU card addition |
| Finance AI / Invoice Matching | Heavy PostgreSQL joins, more AI calls | PostgreSQL scales with RAM; read replica when needed |

**The RAM expandability to 512 GB — 1 TB is the single most important specification.**
Vector databases grow linearly with data. If you build 5 AI systems each requiring 40 GB for their knowledge base, you need 200 GB for AI memory alone before any operating system or application overhead. The production server must have the DIMM slots available to expand without replacing hardware.

---

## Bottom Line for Your Server Supplier Meeting

Tell them:

> We are running a 24/7 AI-driven business automation platform serving leads, vendors, and procurement operations for an aerospace company. We need enterprise-grade hardware: dual hot-swap PSU, hot-swap RAID 10 on NVMe, remote KVM management via iDRAC or iLO, and a dual-socket motherboard. We are building additional AI workloads over the next 2 years — vector databases, document OCR pipelines, and image recognition. RAM expandability to 1 TB is a hard requirement. Warranty must include on-site next-business-day hardware replacement. We cannot have this system go down.
