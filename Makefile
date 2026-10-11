# WhoDoo — everyday commands. Run `make` or `make help` to list them.

VENV    := .venv
PY      := $(VENV)/bin/python
PORT    ?= 8000
# Homebrew's Postgres 16 is not on PATH by default, so call its tools by full path.
PG_BIN  ?= $(shell brew --prefix postgresql@16 2>/dev/null)/bin

.DEFAULT_GOAL := help
.PHONY: help setup install env db-start db-stop db-create migrate migrate-pending seed seed-finance seed-events seed-modules modules seed-all run test test-events test-ledger test-expenses test-approvals test-ocr test-merch test-pages test-frontend test-fairseat test-modules test-member2 test-registrations purge-registrations email-test test-web test-all fairseat-vectors payments stripe-listen receipts psql clean

help: ## Show this list
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# --- First-time setup ---------------------------------------------------------

setup: install env db-start db-create seed ## First-time setup: dependencies, .env, databases and demo data

install: $(VENV)/.installed ## Create the virtual environment and install dependencies

$(VENV)/.installed: requirements.txt
	python3 -m venv $(VENV)
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt
	@touch $@

env: ## Create .env from .env.example if it does not exist
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")

# --- Database -------------------------------------------------------------------

db-start: ## Start the local Postgres service
	brew services start postgresql@16
	@for i in 1 2 3 4 5 6 7 8 9 10; do $(PG_BIN)/pg_isready -q && break; sleep 1; done; $(PG_BIN)/pg_isready

db-stop: ## Stop the local Postgres service
	brew services stop postgresql@16

db-create: ## Create the whodoo and whodoo_test databases if missing
	@$(PG_BIN)/psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = 'whodoo'" | grep -q 1 || $(PG_BIN)/createdb whodoo
	@$(PG_BIN)/psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = 'whodoo_test'" | grep -q 1 || $(PG_BIN)/createdb whodoo_test
	@echo "Databases ready: whodoo, whodoo_test"

migrate: install ## Apply pending SQL migrations to the database in .env
	$(PY) -m scripts.migrate

migrate-pending: migrate ## Same as migrate (the modules' tables are ordinary migrations now)

seed-modules: install ## Add demo plans, tickets on sale, announcements and handover notes (never deletes; runs once)
	$(PY) -m scripts.seed_modules_demo

modules: migrate seed-modules ## Add the module demo data to a database that already has the rest

seed-all: seed ## Same as seed

seed: seed-finance seed-events seed-modules ## Wipe the local database and load all demo data (finance, events, then memberships, tickets, announcements)

seed-finance: install ## Wipe the local database and load people, money and merchandise demo data
	$(PY) -m scripts.seed_finance_demo --reset

seed-events: install ## Add event schedules and volunteer tasks to the seeded club (never deletes; runs once)
	$(PY) -m scripts.seed_events_demo

psql: ## Open a SQL prompt on the local whodoo database
	$(PG_BIN)/psql -d whodoo

# --- Run and test -----------------------------------------------------------------

run: install ## Start the app with auto-reload at http://localhost:8000 (PORT=xxxx to change)
	$(VENV)/bin/uvicorn app.main:app --reload --port $(PORT)

test: install ## Run all tests (uses the whodoo_test database)
	$(PY) -m pytest -q

test-web: ## Run the web app checks: unit tests, type check and production build (needs Node 24+)
	cd web && npm ci && npm test && npm run typecheck && npm run build

test-all: test test-web ## Run everything: backend tests, then the web app checks

test-events: install ## Run only the events, tasks, Autopilot and SyncSphere tests (Member 2)
	$(PY) -m pytest -q tests/test_events.py tests/test_tasks.py tests/test_autopilot.py tests/test_syncsphere.py tests/test_event_pages.py

test-ledger: install ## Run only the TrustLedger tests
	$(PY) -m pytest -q tests/test_ledger.py

test-expenses: install ## Run only the Snap-to-Reimburse tests
	$(PY) -m pytest -q tests/test_expenses.py

test-approvals: install ## Run only the approval policy tests (two-person rule)
	$(PY) -m pytest -q tests/test_approval_policy.py

test-ocr: install ## Run only the receipt reading (OCR) tests
	$(PY) -m pytest -q tests/test_receipt_ocr.py

test-merch: install ## Run only the MerchFlow and payment tests
	$(PY) -m pytest -q tests/test_merch.py

test-frontend: install ## Run only the landing, sign-in, home and navigation tests
	$(PY) -m pytest -q tests/test_frontend.py

test-fairseat: install ## Run only the FairSeat tests: booking, event settings, integration, payments, pricing (uses whodoo_test)
	$(PY) -m pytest -q tests/test_fairseat.py tests/test_fairseat_integration.py tests/test_event_tickets.py tests/test_fairseat_payments.py tests/test_fairseat_pricing.py

test-modules: install ## Run only the MemberCore, GateSync, Connect, CertiDoo and LegacyVault tests (uses whodoo_test)
	$(PY) -m pytest -q tests/test_modules.py

test-member2: install ## Run only the tests for Member 2's features: profile and UPI, reminders, bell, budget, task review, certificate
	$(PY) -m pytest -q tests/test_member2.py tests/test_assignment_review.py

test-registrations: install ## Run only the public registration tests: venues, limit, payment, QR, email, retention
	$(PY) -m pytest -q tests/test_registrations.py

email-test: install ## Send one sample ticket email to check the mail settings in .env: make email-test TO=you@example.com
	$(PY) -m scripts.send_test_email $(TO)

purge-registrations: install ## Delete participant details of events that ended more than 60 days ago (the app also does this daily)
	$(PY) -m scripts.purge_registrations

fairseat-vectors: ## Regenerate ticket price test vectors from MemberCore's TypeScript calculator (needs node 22.6+)
	node --experimental-strip-types --no-warnings scripts/make_ticket_discount_vectors.mjs

test-pages: install ## Run only the HTML page tests
	$(PY) -m pytest -q tests/test_pages.py

# --- Payments -----------------------------------------------------------------------

payments: install ## Show which payment provider is active (Stripe test mode or mock)
	@$(PY) -c "from app.services import payment_service as p; print('Payment provider:', p.get_provider().name)"

stripe-listen: ## Forward Stripe test webhooks to the local server (needs the Stripe CLI)
	@command -v stripe >/dev/null || (echo "Install the Stripe CLI first: brew install stripe/stripe-cli/stripe"; exit 1)
	stripe listen --forward-to http://127.0.0.1:$(PORT)/api/v1/payments/stripe/webhook

# --- Housekeeping -------------------------------------------------------------------

receipts: install ## Redraw the sample receipt images used in the demo
	$(PY) -m scripts.make_sample_receipts

clean: ## Remove caches (keeps .venv, .env, uploads and the database)
	find . -name __pycache__ -not -path "./$(VENV)/*" -prune -exec rm -rf {} +
	rm -rf .pytest_cache
