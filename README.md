<div align="center">

# WHODOO
### *Your campus. One platform.*

<p align="center">
  <img src="docs/assets/branding/whodoo-readme-banner.png" alt="WhoDoo Banner" width="100%" />
</p>

<p align="center">
  <img src="docs/assets/animations/whodoo-intro.gif" alt="Who does what? WhoDoo knows." width="600" />
</p>

<p align="center">
  <strong>An all-in-one extracurricular operations platform for college student organizations.</strong><br />
  Event operations • Volunteer intelligence • Immutable double-entry ledger • Concurrency-safe ticketing • GateSync QR check-in • Official club store
</p>

<p align="center">
  <a href="#-quickstart"><strong>Quickstart</strong></a> •
  <a href="#-inside-whodoo--visual-walkthrough"><strong>Visual Walkthrough</strong></a> •
  <a href="#-core-modules--verified-architecture"><strong>Architecture</strong></a> •
  <a href="#-hackathon-demo-script"><strong>Demo Script</strong></a> •
  <a href="#-testing--verification"><strong>Tests</strong></a> •
  <a href="#-in-depth-documentation"><strong>Docs</strong></a>
</p>

---

<p align="center">
  <img src="docs/assets/branding/whodoo-homepage.png" alt="WhoDoo Official Homepage" width="94%" />
</p>

</div>

---

## 📌 The Problem

College student clubs are the lifeblood of campus culture, yet their daily operations are fragmented across chaotic, disconnected tools:

- **Lost Accountability:** Registrations live in Google Forms, budget receipts get buried in WhatsApp chats, and cash/UPI payments lack traceable confirmation.
- **Volunteer Coordination Breakdowns:** Organizers struggle to track who is working which shift, leading to double-booked venues, overlooked tasks, and last-minute scrambles.
- **Ticketing & Admission Fraud:** Shared screenshots of confirmation emails and static QR codes lead to duplicate admissions at crowded auditorium gates.
- **No Handover Memory:** When leadership graduates each year, historical budgets, vendor contacts, and club wisdom vanish into personal drives.

---

## 💡 The Solution: WhoDoo

**WhoDoo brings the entire student organization into a single, cohesive ecosystem.**

Instead of duct-taping spreadsheets to chat apps, WhoDoo unifies **campus event discovery**, **volunteer task dispatch with blocker escalation**, an **auditable financial ledger with receipt OCR**, **concurrency-locked ticket reservations**, **real-time camera QR admission**, and an **official club merchandise store**.

```
   ┌───────────────────────────────────────────────────────────────┐
   │                       WHODOO ECOSYSTEM                        │
   ├───────────────┬───────────────────────────────┬───────────────┤
   │  STUDENTS     │     VOLUNTEERS & LEADS        │  LEADERSHIP   │
   │  • Discovery  │  • CrewPulse Task Board       │  • President  │
   │  • FairSeat   │  • SyncSphere Clash Detection │  • Treasurer  │
   │  • GateSync   │  • GateSync Mobile Scanner    │  • Ledger P&L │
   │  • Club Store │  • Snap-to-Reimburse OCR      │  • Handover   │
   └───────────────┴───────────────────────────────┴───────────────┘
```

---

## 🚀 Key Modules & Implemented Capabilities

| Module | What It Does | Verified Implementation |
|---|---|---|
| **TrustLedger** | Immutable append-only double-entry financial ledger recording income, expense claims, ticket revenue, and reimbursements. | `app/services/ledger_service.py`<br />`migrations/001_ledger.sql` |
| **Snap-to-Reimburse** | Volunteer receipt upload with image OCR parsing vendor, date, and amount; enforces a multi-tier approval policy (two-person rule). | `app/services/expense_service.py`<br />`app/services/ocr_service.py` |
| **FairSeat** | Ticket reservation engine with strict concurrency locks, seat quotas, and member discount calculations. | `app/services/fairseat_service.py`<br />`app/services/fairseat_pricing.py` |
| **GateSync** | In-browser camera QR code scanner with offline fallback, ticket redemption API, SHA-256 digest checks, and instant duplicate detection. | `web/src/modules/gatesync/`<br />`app/static/door.js` |
| **CrewPulse** | Volunteer task tracking with deadlines, critical priority flags, blocker notes, and activity audit trails. | `app/services/task_service.py`<br />`migrations/011_crewpulse.sql` |
| **Event Autopilot** | Deterministic 0–100 readiness scoring calculated from real task statuses, venue conflicts, and budget allocations. | `app/services/autopilot.py` |
| **SyncSphere** | Campus conflict detection identifying double-booked venues, overlapping lead schedules, and suggested alternate slots. | `app/services/syncsphere.py` |
| **MerchFlow** | Official club store featuring member-exclusive pricing, live stock indicators, and atomic inventory reservation. | `app/services/merch_service.py`<br />`migrations/003_merch.sql` |
| **MemberCore** | Tiered membership plans, member discount basis-point calculation, and Supabase ES256 identity mapping bridge. | `app/services/modules.py`<br />`app/core/supabase_auth.py` |
| **CertiDoo & LegacyVault** | Verifiable A4 printable certificates and structured officer handover documentation for club continuity. | `app/services/modules.py`<br />`migrations/035_036` |

---

## 📸 Inside WhoDoo — Visual Walkthrough

### 1. Campus Event Discovery & Calendar
> *Every club’s upcoming events in one unified, public schedule. Students browse what is on, filter by date, view availability, and book tickets without needing an initial account.*

<p align="center">
  <img src="docs/assets/screenshots/whodoo-calendar.png" alt="WhoDoo Public Event Calendar" width="94%" />
</p>

---

### 2. One Platform, Different Responsibilities
> *WhoDoo reflects the real structure of campus organizations: President, Treasurer, Event Leads, Members, and Volunteers. Each role receives scoped capabilities, strict approval boundaries, and dedicated operational views.*

<p align="center">
  <img src="docs/assets/screenshots/whodoo-roles.png" alt="WhoDoo Role Hierarchy and Access" width="70%" />
</p>

---

### 3. From Booking to the Door: GateSync QR Check-in
> *Tickets are validated in real time at event entry. GateSync activates the device camera, parses the cryptographic ticket digest, updates the live attendance counter, and immediately flags repeat check-in attempts to prevent pass-backs.*

<p align="center">
  <img src="docs/assets/screenshots/whodoo-qr-checkin.png" alt="GateSync QR Scanner with Duplicate Check-in Prevention" width="70%" />
</p>

*Above: A live scan in CS Lab 2 detecting a duplicate scan attempt (`Already checked in — WD-K6Q5BV`), preserving gate integrity while maintaining an instant guest arrival log.*

---

### 4. TrustLedger: Transparent Event Finances
> *Total visibility into club cash flow. Organizers and Treasurers track ticket revenue, registration income, approved reimbursements, and remaining budget balances directly derived from immutable ledger rows.*

<p align="center">
  <img src="docs/assets/screenshots/whodoo-finance-dashboard.png" alt="TrustLedger Event Financial Dashboard" width="75%" />
</p>

---

### 5. MerchFlow: Official Club Store
> *Members unlock exclusive pricing across official club apparel and merchandise. Built with concurrency-safe stock decrementing so items are never oversold.*

<p align="center">
  <img src="docs/assets/screenshots/whodoo-club-store.png" alt="MerchFlow Club Merchandise Store" width="94%" />
</p>

#### Official Merchandise Lineup

| Club Hoodie | Club T-Shirt | Enamel Mug |
| :---: | :---: | :---: |
| <img src="docs/assets/merch/whodoo-hoodie.png" alt="WhoDoo Club Hoodie" width="260" /> | <img src="docs/assets/merch/whodoo-tshirt.png" alt="WhoDoo Club T-Shirt" width="260" /> | <img src="docs/assets/merch/whodoo-enamel-mug.png" alt="WhoDoo Enamel Mug" width="260" /> |
| **₹799.00** <del>₹999.00</del> | **₹399.00** <del>₹499.00</del> | **₹249.00** <del>₹299.00</del> |
| *Active Member Price* | *Active Member Price* | *Active Member Price* |

---

## 🏛 Verified Architecture

WhoDoo enforces a clean separation of concerns between its **FastAPI backend services & server-rendered Jinja2 interface** and its **Next.js frontend client foundation**:

```mermaid
graph TD
    subgraph ClientLayer["Frontend & Client Interfaces"]
        Browser["Desktop & Mobile Browser"]
        NextApp["Next.js Web App (web/)<br/>React 19 • Tailwind • TypeScript"]
        JinjaApp["Jinja2 Admin UI (app/templates/)<br/>Server-Rendered HTML • ZXing Scanner"]
    end

    subgraph ApiGateway["FastAPI Application (app/)"]
        AuthMiddleware["Auth Bridge (Local HS256 / Supabase ES256)"]
        Router["API Router (/api/v1) & Web Router"]
    end

    subgraph DomainServices["Core Domain Services (app/services/)"]
        FairSeatSvc["FairSeat Engine<br/>Inventory & Seat Hold Locks"]
        LedgerSvc["TrustLedger Service<br/>Append-Only Transactions"]
        ExpenseSvc["Expense & OCR Service<br/>Receipt Extraction & 2-Person Rules"]
        TaskSvc["CrewPulse & SyncSphere<br/>Task Dispatch & Clash Detection"]
        MerchSvc["MerchFlow Service<br/>Inventory Atomic Update"]
    end

    subgraph Persistence["Storage & Database"]
        PG[(PostgreSQL 14+<br/>asyncpg & SQLAlchemy Async)]
        Migrations["SQL Migrations (000 to 045)<br/>Immutability Triggers & Partition Keys"]
        LocalUploads["Local Receipts / Supabase Storage"]
    end

    Browser -->|HTTP / HTML| JinjaApp
    Browser -->|HTTP / React| NextApp
    JinjaApp --> Router
    NextApp --> Router
    Router --> AuthMiddleware
    AuthMiddleware --> FairSeatSvc
    AuthMiddleware --> LedgerSvc
    AuthMiddleware --> ExpenseSvc
    AuthMiddleware --> TaskSvc
    AuthMiddleware --> MerchSvc
    FairSeatSvc --> PG
    LedgerSvc --> PG
    ExpenseSvc --> PG
    ExpenseSvc --> LocalUploads
    TaskSvc --> PG
    MerchSvc --> PG
    Migrations -.->|Enforces Schema| PG
```

### End-to-End Ticketing & Admission Lifecycle

```mermaid
sequenceDiagram
    autonumber
    actor Student
    participant Store as Calendar / FairSeat
    participant Engine as FairSeat Service
    participant DB as PostgreSQL
    actor Gate as Volunteer Scanner
    participant Scanner as GateSync Scanner

    Student->>Store: Request ticket reservation
    Store->>Engine: Hold seat with row lock
    Engine->>DB: Check quota & create reservation hold
    Student->>Store: Confirm booking (Mock / Stripe)
    Store->>DB: Issue ticket with opaque token & store SHA-256 digest
    Student->>Student: Receives QR code containing opaque credential
    Note over Student,Gate: Event Day at Venue Entrance
    Gate->>Scanner: Open camera scanner
    Student->>Scanner: Present QR ticket on mobile screen
    Scanner->>Engine: Verify ticket digest against database
    alt First valid presentation
        Engine->>DB: Set checked_in=true & log timestamp
        Scanner-->>Gate: Status: CHECKED IN (Green indicator)
    else Re-scanned presentation
        Engine->>DB: Detect existing checked_in=true flag
        Scanner-->>Gate: Status: ALREADY CHECKED IN (Yellow warning banner)
    end
```

---

## 💻 Verified Tech Stack

| Layer | Technologies | Purpose in WhoDoo |
|---|---|---|
| **Backend Framework** | **FastAPI** (`0.143.0`), **Uvicorn** (`0.54.0`) | High-performance asynchronous REST API and route handlers |
| **Backend Database** | **PostgreSQL 14+**, **SQLAlchemy** (`2.1.4` Async), **asyncpg** (`0.32.0`) | Transactional persistence, row-level locks, immutability triggers |
| **Backend UI & Templating** | **Jinja2** (`3.1.6`), **Vanilla CSS** design system | Server-rendered portal, Anton/Inter typography, responsive panels |
| **Frontend Framework** | **Next.js 16.4.0** (Turbopack), **React 19**, **Tailwind CSS 4** | Modular web foundation, App Router, responsive components |
| **Security & Auth** | **PyJWT**, **cryptography** (scrypt hashes), **Supabase Auth Bridge** | Dual-mode auth: Local HS256 tokens and Supabase ES256 JWKS |
| **Receipt OCR** | **pytesseract** (`0.3.13`), **Pillow** (`12.3.0`) | Automated parsing of vendor, total amount, and date from receipts |
| **QR & Scanning** | **Segno** (`1.6.6`), **@zxing/browser**, native BarcodeDetector | QR generation for tickets and in-browser camera decoding |
| **Payments** | **Stripe Checkout** (`16.0.0`) test mode, **Mock Provider** | Pluggable payments architecture with webhook reconciliation |
| **Testing Suites** | **pytest** (`9.1.1`), **pytest-asyncio**, native **Node.js test runner** | 158 frontend tests + full backend integration test suite |

---

## ⚡ Quickstart

### Prerequisites
- **Python 3.13+**
- **Node.js 24+**
- **PostgreSQL 14+** (databases `whodoo` and `whodoo_test`)
- *(Optional)* Tesseract OCR for receipt image processing

### 1. Clone & Configure Environment

```bash
git clone https://github.com/vishnubhargavd/WhoDoo.git
cd WhoDoo

# Create local environment file from verified example
cp .env.example .env
```

### 2. Set Up Python Backend

```bash
# Create and activate virtual environment
python -m venv .venv

# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# On macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Initialize Database & Demo Records

Ensure your local PostgreSQL server is active with the `whodoo` database created:

```bash
# Apply all 25 versioned SQL migrations (000 to 045)
python -m scripts.migrate

# Seed demo data: finance, events, module data, and secure credentials
python -m scripts.seed_finance_demo --reset
python -m scripts.seed_events_demo
python -m scripts.seed_modules_demo
python -m scripts.setup_credentials --reset
```

*(On macOS with Homebrew, `make setup` executes this entire sequence automatically).*

### 4. Run the Applications

**FastAPI Backend & Admin Portal** (starts at `http://localhost:8000`):
```bash
python -m uvicorn app.main:app --reload --port 8000
```
- Interactive API Documentation: `http://localhost:8000/docs`
- Sign In Portal: `http://localhost:8000/login`

**Next.js Web Client** (starts at `http://localhost:3000`):
```bash
cd web
npm ci
npm run dev
```

---

## 🧪 Testing & Verification

WhoDoo enforces thorough testing across backend business logic, financial immutability, and frontend contracts:

```bash
# 1. Run Next.js Frontend Unit & Integration Tests (158 passing tests)
cd web
npm test

# 2. Run TypeScript strict type verification
npm run typecheck

# 3. Test production build bundle
npm run build
```

### Parity Test Vectors
To guarantee that the Python pricing engine (`app/services/fairseat_pricing.py`) and the TypeScript MemberCore calculator (`web/src/modules/membercore/ticket-discount.ts`) never drift, shared price vectors are regenerated and verified:

```bash
# Generate 379 test vectors across edge-case discounts and prices
node --experimental-strip-types --no-warnings scripts/make_ticket_discount_vectors.mjs
```

### Backend Test Suite
Backend tests run against the isolated `whodoo_test` database:
```bash
# Run all backend tests
python -m pytest -q

# Run specific domain test suites
python -m pytest tests/test_fairseat_pricing.py   # Pricing parity
python -m pytest tests/test_ledger.py             # Ledger immutability
python -m pytest tests/test_approval_policy.py    # Two-person expense rules
python -m pytest tests/test_registrations.py       # GateSync QR contracts
```

---

## 🎯 Reviewer & Hackathon Demo Script (2 Minutes)

Follow this verified demonstration script to experience WhoDoo’s connected workflows:

1. **Event Discovery:** Visit `http://localhost:8000` and view the October 2026 calendar. Click **Kamutala bday** to inspect seat limits and ticket availability.
2. **Role-Based Login:** Go to `/login` and select **Ananya Rao** (Club President) or **Rohan Mehta** (Treasurer). Enter the credentials generated in `credentials/`.
3. **Financial Transparency:** Navigate to `/finance` → click into **Bysani's Reception Day** (`/treasurer/events/<id>`). Inspect live Money In, Money Out, approved reimbursements, and the immutable ledger log.
4. **Expense OCR:** Navigate to `/expenses/new`, upload a sample receipt from `app/static/samples/receipts/`, watch the OCR extract vendor and total amounts, and submit for two-person review.
5. **GateSync QR Check-In:** Navigate to `/events` → choose an event → click **Check-in**. Use your phone camera to scan a generated ticket QR and watch the attendance counter increment in real time with duplicate detection.
6. **Club Merchandise:** Visit `/store` to see member discounts applied automatically to the Club Hoodie, T-Shirt, and Mug.

---

## 📂 Project Structure

```
WhoDoo/
├── app/                  # FastAPI backend, domain services & Jinja2 admin views
│   ├── api/v1/           # Modular REST API endpoints (ledger, expenses, fairseat)
│   ├── core/             # Configuration, scrypt/JWT security, Supabase ES256 bridge
│   ├── db/               # PostgreSQL connection engine & asyncpg migration runner
│   ├── models/           # SQLAlchemy models for all domain entities
│   ├── schemas/          # Pydantic request/response validation schemas
│   ├── services/         # Domain business logic (ledger, fairseat, tasks, ocr)
│   ├── static/           # Design system tokens, brand assets, and client JS
│   ├── templates/        # Server-rendered Jinja2 HTML views
│   └── web/              # FastAPI route handlers rendering HTML templates
├── web/                  # Next.js web application (React 19, TypeScript, Tailwind)
│   ├── src/app/          # App Router pages (account, bookings, check-in, memberships)
│   ├── src/lib/          # Supabase SSR client, callback proxy & configuration
│   ├── src/modules/      # FairSeat portal, GateSync scanner, MemberCore identity
│   └── tests/            # Native Node test runner suite (158 passing tests)
├── migrations/           # 25 canonical PostgreSQL migrations (000 to 045)
├── scripts/              # Migration runner, demo seeders, test vector generator
├── tests/                # Pytest backend test suite & fixtures
├── docs/                 # Consolidated architecture & module documentation
│   ├── assets/           # Real screenshots, branding banner, animated GIF & merch
│   ├── AUTH_BRIDGE.md    # Supabase ES256 authentication bridge specification
│   ├── FAIRSEAT.md       # FairSeat ticket reservation engine & pricing rules
│   ├── MEMBER2_EVENT_OPERATIONS.md # Event operations, CrewPulse, Autopilot, SyncSphere
│   ├── MEMBER3_FINANCE.md# TrustLedger, Snap-to-Reimburse, MerchFlow, Payments
│   └── MODULES.md        # MemberCore, GateSync, CertiDoo, LegacyVault
├── Makefile              # Project task automation
├── pytest.ini            # Pytest configuration
└── requirements.txt      # Python dependencies
```

---

## 🔒 Security, Privacy & Integrity Notes

- **Credentials & Passwords:** `credentials/` is strictly ignored by `.gitignore`. The database stores salted `scrypt` password hashes (`user_credentials`, migration `045`). Five consecutive failed sign-in attempts lock an account for five minutes.
- **Financial Immutability:** Ledger transactions are append-only. Triggers prevent `UPDATE` and `DELETE` operations on historical ledger entries.
- **Privacy in QR Documentation:** Public documentation screenshots have background camera faces gently redacted to protect student privacy while keeping the GateSync scanner UI and duplicate detection message completely intact.
- **Payment Providers:** By default, `PAYMENT_PROVIDER=mock` provides zero-dependency local testing without external network calls. Setting Stripe API keys switches seamlessly to Stripe Checkout test mode.

---

## 📖 In-Depth Documentation

For detailed technical specifications, refer to the documentation in [`docs/`](docs/):

- 📘 [**docs/MEMBER3_FINANCE.md**](docs/MEMBER3_FINANCE.md) — TrustLedger, Snap-to-Reimburse, MerchFlow, Payments, and Jinja UI design.
- 📗 [**docs/MEMBER2_EVENT_OPERATIONS.md**](docs/MEMBER2_EVENT_OPERATIONS.md) — Event Management, CrewPulse, Event Autopilot, and SyncSphere.
- 📙 [**docs/FAIRSEAT.md**](docs/FAIRSEAT.md) — FairSeat ticket reservation engine, lock ordering, and pricing quotes.
- 📕 [**docs/AUTH_BRIDGE.md**](docs/AUTH_BRIDGE.md) — Supabase Auth bridge and ES256 JWKS token verification policy.
- 📓 [**docs/MODULES.md**](docs/MODULES.md) — MemberCore identity, GateSync check-in contracts, CertiDoo, and LegacyVault.
- 📁 [**docs/membercore/phase3c/**](docs/membercore/phase3c/) — Phase 3C handoff package, SQL assertions, and security specs.

---

<div align="center">

**WHODOO — YOUR CAMPUS. ONE PLATFORM.**

<sub>Crafted for college student organizations with care, engineering rigor, and attention to detail.</sub>

</div>
