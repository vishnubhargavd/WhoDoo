# WhoDoo — Finance, Reimbursements & Commerce module (Member 3)

WhoDoo is an extracurricular club management system for college students. This is one part of it: a FastAPI backend for the money side of a club, with plain HTML pages for review.

| Module | What it does |
|---|---|
| **TrustLedger** | One immutable ledger for all income, expenses, reimbursements and refunds. Treasurer dashboard, filters, event profit and loss. |
| **Snap-to-Reimburse** | Upload a receipt, OCR reads vendor, amount and date, the volunteer confirms, approvers approve, the treasurer pays. |
| **MerchFlow** | Merchandise catalogue with member pricing, orders, and stock that cannot be oversold. |
| **Payments** | Stripe Checkout (test mode): checkout session → server-side verification → webhook. Falls back to a **Mock Payment (Stripe-compatible)** provider when no Stripe test key is set. |

## Quick start

Needs Python 3.13, PostgreSQL 14+ and (optionally) Tesseract.

```bash
brew install postgresql@16 tesseract
make setup      # virtual environment, .env, databases and demo data
make run        # http://localhost:8000
```

Open <http://localhost:8000> and pick a user on the demo sign-in page. API docs are at `/docs`.

Everyday commands (run `make help` for the full list):

| Command | What it does |
|---|---|
| `make run` | Start the app with auto-reload (`make run PORT=8080` to change the port) |
| `make test` | Run all 142 tests against the `whodoo_test` database |
| `make test-ledger`, `test-expenses`, `test-approvals`, `test-ocr`, `test-merch`, `test-pages` | Run one area's tests |
| `make seed` | Wipe the local database and load fresh demo data |
| `make migrate` | Apply pending SQL migrations |
| `make db-start`, `make db-stop` | Start or stop the local Postgres service |
| `make psql` | Open a SQL prompt on the local database |

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://localhost/whodoo` | Local Postgres or the Supabase connection string (`postgres://` and `postgresql://` are accepted). |
| `DB_DISABLE_STATEMENT_CACHE` | `0` | Set to `1` when using the Supabase transaction pooler (port 6543). |
| `DEMO_AUTH` | `0` | `1` enables `/dev/login`. Keep it off outside demos. |
| `JWT_SECRET` | — | Signs demo session tokens. Required when `DEMO_AUTH` is off and no Supabase secret is set. |
| `SUPABASE_JWT_SECRET` | — | Set this to accept real Supabase Auth tokens instead. |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_STORAGE_BUCKET` | — / — / `receipts` | Receipt storage. Blank means receipts go to local disk under `UPLOAD_DIR`. |
| `UPLOAD_DIR` | `uploads` | Local receipt folder. |
| `EXPENSE_AUTO_APPROVAL_LIMIT` | `1000` | Claims at or above this need two approvals. |
| `PAYMENT_PROVIDER` | `auto` | `stripe` or `auto` use Stripe when `STRIPE_SECRET_KEY` is a test key, otherwise the mock. `mock` forces the mock. |
| `STRIPE_SECRET_KEY` | — | Stripe **test-mode** secret key (`sk_test_…`). Server only. Live keys are refused. |
| `STRIPE_PUBLISHABLE_KEY` | — | Stored for completeness. Hosted Checkout by redirect does not need it in the browser. |
| `STRIPE_WEBHOOK_SECRET` | — | `whsec_…` from `make stripe-listen`. Optional: without it webhooks are rejected and checkout still completes. |
| `APP_BASE_URL` | `http://127.0.0.1:8000` | Where Stripe sends the buyer back to. Must match the address you open the app on. |

### Moving to Supabase

1. Put the Supabase connection string in `DATABASE_URL` and run `python -m scripts.migrate`.
2. Set `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`, and create a public `receipts` bucket.
3. Set `SUPABASE_JWT_SECRET` and turn `DEMO_AUTH` off once real sign-in exists.

The migrations are plain SQL with no extensions, so they run unchanged on Supabase.

## Database schema

Migrations live in `migrations/` and are applied in name order by `python -m scripts.migrate`.

| File | Tables |
|---|---|
| `000_shared_placeholders.sql` | `clubs`, `users`, `events` — **temporary stand-ins** for tables owned by Members 1 and 2 |
| `001_ledger.sql` | `ledger_entries`, `approval_logs`, `audit_logs` |
| `002_expenses.sql` | `expense_claims`, `receipt_uploads` |
| `003_merch.sql` | `merch_products`, `product_variants`, `merch_orders`, `order_items` |
| `004_stripe_payments.sql` | Renames the provider ids on `merch_orders` to `stripe_checkout_session_id` and `stripe_payment_intent_id`, adds `currency`, creates `payment_events` |

The tables follow the brief. Additions, and why:

| Addition | Reason |
|---|---|
| `ledger_entries.idempotency_key` (unique) | A retried webhook or double click cannot post an entry twice, while a refund or adjustment for the same reference still can. Payment-driven keys are `<stripe_payment_intent_id>:<entry_type>`. |
| Trigger `ledger_entries_immutable` | The database itself rejects deletes, and any update other than `payment_status`. Corrections are new `adjustment` entries. |
| `ledger_entries.source_module` also allows `fundraiser` | The summary reports fundraiser income. |
| `expense_claims.approval_stage`, `first_approved_by` | Track which of the two approvals a claim is waiting for, and stop one person giving both. |
| `receipt_uploads` | Holds the stored file and OCR result between upload and claim, so a claim's `extracted_*` fields never come from the browser. |
| `merch_orders.event_id` | Lets merchandise sold for an event count in that event's profit and loss. |
| `audit_logs` | Payment status changes, manual ledger entries, stock changes. |
| `CHECK (stock_quantity >= 0)` | Last line of defence against overselling. |

Finance tables hold plain UUIDs for users, clubs and events, with no foreign keys to the placeholder tables, so those can be swapped without touching finance data.

## API

All routes are under `/api/v1` and need a signed-in user (`Authorization: Bearer <jwt>` or the session cookie), except the webhook. Errors always look like:

```json
{ "error": { "code": "self_approval", "message": "You cannot decide on your own claim", "details": null } }
```

### TrustLedger — treasurer and president

| Method | Path | Notes |
|---|---|---|
| GET | `/ledger/summary` | Balance, income, expenses, reimbursements paid and owed, refunds, income by source, expenses by category, monthly trend |
| GET | `/ledger/entries` | Filters: `date_from`, `date_to`, `entry_type`, `source_module`, `event_id`, `payment_status`, `category`, `q`, `limit`, `offset` |
| GET | `/ledger/events/{event_id}/profit-loss` | Ticket and merchandise income, approved expenses, paid reimbursements, net result, tickets sold |
| POST | `/ledger/manual-entry` | Income, expense, or adjustment (adjustments need a `direction`) |

How totals are worked out:

- Only `paid` entries move `current_balance`. An approved, unpaid reimbursement shows under `pending_reimbursements`.
- `total_expenses` includes reimbursements; `total_reimbursements_paid` is that part of it.
- Event profit and loss counts a cost as soon as it is approved, even before it is paid out.
- `tickets_sold` is the number of ticket income entries for the event. `attendees` is `null` until check-in data exists.

### Snap-to-Reimburse

| Method | Path | Who |
|---|---|---|
| POST | `/expenses/ocr` | Anyone. Multipart `file`. Stores the receipt, runs OCR, returns `receipt_id` and extracted fields. Creates no claim. |
| POST | `/expenses` | Anyone. Needs `receipt_id`, title, description, category, amount, expense date. |
| GET | `/expenses` | Your own claims (`?status=`) |
| GET | `/expenses/approvals` | Approvers: `to_approve`, plus `to_pay` for treasurer and president |
| GET | `/expenses/{id}` | Submitter, their approvers, treasurer, president. Includes OCR data, approval history, ledger status, `allowed_actions`. |
| POST | `/expenses/{id}/approve` | Approvers |
| POST | `/expenses/{id}/reject` | Approvers. `reason` required. |
| POST | `/expenses/{id}/request-changes` | Approvers. `comment` required. |
| POST | `/expenses/{id}/resubmit` | Submitter, after changes were requested. Approval starts over. *(Added to the brief.)* |
| POST | `/expenses/{id}/mark-paid` | Treasurer or president |

### MerchFlow

| Method | Path | Who |
|---|---|---|
| GET | `/merch/products`, `/merch/products/{id}` | Anyone. `your_price` reflects the caller's membership. |
| POST / PUT | `/merch/products`, `/merch/products/{id}` | Treasurer, president |
| POST | `/merch/products/{id}/variants` | Treasurer, president |
| PUT | `/merch/variants/{id}` | Treasurer, president (stock, price override, active) |
| POST | `/merch/orders` | Anyone. Body is variant ids and quantities only. |
| GET | `/merch/orders`, `/merch/orders/{id}` | Your own orders; `?all=true` for treasurer and president |
| POST | `/merch/orders/{id}/status` | Treasurer, president: `packed`, `delivered`, `cancelled` |

### Payments

| Method | Path | Notes |
|---|---|---|
| POST | `/payments/stripe/create-checkout-session` | Body: `merch_order_id`. Opens a Checkout Session for your unpaid order; the server sets the amount. Returns `checkout_url`. |
| POST | `/payments/stripe/verify` | Body: `session_id`. Retrieves the session from Stripe, checks it is paid, yours and for the right amount, then fulfils the order. |
| POST | `/payments/stripe/webhook` | `checkout.session.completed`, `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.refunded`. Authenticated by `Stripe-Signature`. |
| POST | `/payments/mock/simulate` | Mock provider only. Stands in for the buyer on Stripe's checkout page (`success` or `failure`). |

## Approval workflow

```
submit ──► pending_approval (first) ──approve──► approved ──mark-paid──► paid
                  │   amount ≥ limit                ▲
                  └──approve──► pending_approval (final) ──approve by a DIFFERENT treasurer/president──┘
           reject ──► rejected        request-changes ──► change_requested ──resubmit──► back to first
```

- **Below ₹1,000:** one approval, from the event's lead. The treasurer or president can step in.
- **₹1,000 or more:** two approvals from **two different people**.
  - First approval: the event's lead, or a treasurer or president standing in.
  - Final approval: a treasurer or president **who did not give the first approval**.
  - A treasurer or president who approves first has not also given the final approval. A second attempt by the same person gets `409 duplicate_approval`.
- **The submitter always sees where the claim stands.** Every claim carries a `status_label`, e.g. "First approval by Karthik Nair · awaiting final approval from Rohan Mehta", and the claim page shows each step with the approver's name. Open pages refresh themselves within about five seconds of a change.
- **Approvers see why they cannot act.** `approval_blocked_reason` explains it, and the approvals page lists claims they first-approved under "You gave the first approval".
- **The first approver can still reject or send back** a claim they approved; only approving twice is blocked.
- **A change request is shown to the submitter in full.** Their list and claim page say who asked and what for (`reviewer_name`, `reviewer_note`), with an "Edit and resubmit" button.
- **One browser, one user.** The demo sign-in is a single cookie, so signing in as someone else in another tab switches every tab. An open page notices and says so instead of quietly showing the other person's data.
- **First approver:** the linked event's lead. With no event, or when the lead is the submitter, it goes to the treasurer, then the president.
- **Nobody decides on or pays out their own claim.** This applies to the treasurer too.
- **Final approval** writes exactly one ledger entry: `reimbursement`, `debit`, `approved`. Nothing is posted after only a first approval, and a claim cannot be marked paid until it is fully approved.
- **The rule is enforced in the service**, on the row-locked claim, from the stored amount and stored approver ids. Nothing about approvers is accepted from the request.
- **Mark paid** moves that entry to `paid`, which is when the balance drops.
- Every action is written to `approval_logs`. Claims are row-locked while being decided, so a second approval gets `409`.

## OCR flow

1. `POST /expenses/ocr` checks the file's real type from its contents (PNG, JPEG, WebP or PDF, up to 8 MB) and stores it under a random name.
2. The image is put on a white background and enlarged to about 1,200 px **wide** (receipts are tall and narrow), then Tesseract reads it in two layout modes and the fuller reading is kept.
3. Rules in `app/services/ocr_service.py` pick out:
   - **Merchant:** the first line near the top that is a name, skipping bare labels ("Tax Invoice"), labelled details (GSTIN, phone, bill number) and address lines.
   - **Items:** each line above the summary block, with quantity and unit price when printed. Wrapped item names are joined.
   - **Subtotal, tax, discount:** read separately from their own lines. Cash tendered, change and card lines are ignored.
   - **Total:** only ever the figure on a line labelled as the total ("Grand Total", "Net Amount", "Amount Due" … before a plain "Total").
   - **Date:** the first plausible date, read day-first.
4. The response carries `extracted_vendor`, `extracted_amount`, `extracted_date`, `line_items`, `subtotal`, `tax_total`, `discount_total`, `items_total`, `amount_status`, `warnings` and Tesseract's own mean word confidence.
5. The volunteer checks the reading against the receipt, corrects it and submits. Nothing is claimed before that.
6. The claim keeps both the OCR reading and what was submitted. The claim page shows them side by side, marks edited fields, and lists the items read.

Nothing is guessed:

- If the total line is missing or unreadable, `extracted_amount` is `null` and `amount_status` is `not_found`. The amount field is left empty. `suggested_amount` offers what the readable lines add up to, and is only used if the volunteer picks it.
- If the total disagrees with the item lines, the total is reported as printed and a warning says so.
- `ocr_confidence` is how clearly Tesseract read the characters. It is not a measure of whether the fields are right.
- The ledger is never written from OCR. It gets the amount the volunteer confirmed, and only once an approver approves it.

If Tesseract is missing, the file is a PDF, or the image cannot be read, the upload still succeeds with empty fields and zero confidence, and the volunteer types the values in.

## Payment flow

```
POST /merch/orders                               order created (payment pending), stock checked, nothing reserved
POST /payments/stripe/create-checkout-session    Checkout Session opened for the server's amount, in paise
   buyer pays on Stripe's hosted page            (or /payments/mock/simulate with the mock provider)
GET  /orders/{id}?session_id=…                   Stripe's success redirect; the page calls the same check as:
POST /payments/stripe/verify                     session retrieved from Stripe with the secret key
   └─ one transaction: order → paid, stock reduced, merchandise income posted to the ledger
POST /payments/stripe/webhook                    the same settlement, if the buyer never comes back
```

### Stripe Test Mode setup

1. In the Stripe Dashboard, switch to **Test mode** → **Developers → API keys**.
2. Put the keys in `.env` (never in code, never in Git; `.env` is ignored):
   ```env
   PAYMENT_PROVIDER=stripe
   STRIPE_SECRET_KEY=sk_test_…
   STRIPE_PUBLISHABLE_KEY=pk_test_…
   APP_BASE_URL=http://127.0.0.1:8000
   ```
3. `make payments` prints which provider is active. `make run`, then open the app at the same address as `APP_BASE_URL`.
4. Buy something and pay on Stripe's page with the test card **4242 4242 4242 4242**, any future expiry, any CVC, any postal code.
5. Optional, for webhooks: `brew install stripe/stripe-cli/stripe`, `stripe login`, then `make stripe-listen`. Copy the `whsec_…` it prints into `STRIPE_WEBHOOK_SECRET` and restart.

If a secret key has ever been pasted into a chat, a commit or a screenshot, roll it in the Dashboard before using it.

### Idempotency and stock safety

- **Nothing is trusted from the browser.** The amount is the order's own total; the paid status and amount are read from Stripe with the secret key. `merch_order_id` in the session metadata is the link back to the order, and it must match.
- **Stock** is reduced by `UPDATE … WHERE stock_quantity >= quantity`, in the same transaction that marks the order paid and writes the ledger entry.
- **One ledger entry per payment.** The income entry's unique `idempotency_key` is `<payment_intent_id>:income`, and a refund's is `<payment_intent_id>:refund`.
- **Repeats are safe.** The success redirect, a page refresh, another call to verify, and the webhook all end in the same settlement on a row-locked order; once it is paid they change nothing.
- **Webhook deliveries are recorded** in `payment_events`, whose `provider_event_id` is unique. A repeated delivery is acknowledged with `200` and skipped.
- **Webhooks are a second path, not the only one.** With no `STRIPE_WEBHOOK_SECRET`, every webhook is rejected with `400` and the purchase still completes through the redirect.
- **Paid but sold out.** If the last item went to someone else between ordering and paying, the order is cancelled, the PaymentIntent is refunded in full through Stripe, the ledger shows the income and the matching refund, and the buyer gets `409 insufficient_stock`.
- **A declined card** marks the payment `failed` and leaves the order open, because Stripe lets the buyer try another card.
- **Cancelling a paid order** restores stock, refunds through Stripe and posts a `refund` entry; the original income entry is left alone.

### Mock Payment (Stripe-compatible)

With `PAYMENT_PROVIDER=mock`, or without a usable `sk_test_` key, `MockPaymentProvider` is used. It issues `cs_mock_…` sessions and `pi_mock_…` payment ids and verifies webhooks with Stripe's own signature check, so the verify, webhook and ledger code is identical. The checkout page is labelled as mock and offers a success button and a declined-card button. **No real money moves.** If Stripe rejects the configured key, the app logs the error and switches to the mock until it is restarted.

To switch between them, change `PAYMENT_PROVIDER` (and the key) in `.env` and restart. No code changes.

## Pages

| Path | Page |
|---|---|
| `/treasurer` | Dashboard: balance, totals, event profit and loss, trend |
| `/treasurer/ledger` | Ledger with filters and manual entry |
| `/expenses`, `/expenses/new`, `/expenses/{id}` | My claims, submit with OCR, claim detail |
| `/expenses/approvals` | Approval queue and claims waiting for payment |
| `/store`, `/store/{product_id}`, `/checkout/{order_id}`, `/orders`, `/orders/{order_id}` | Store, product, payment, my orders, order confirmation |
| `/admin/merch`, `/admin/merch/orders` | Products, stock, order management |
| `/` | Landing page, for everyone (`/welcome` redirects here) |
| `/home` | A signed-in person's home, with live figures |
| `/login`, `/dev/login` | Sign-in entry point; demo sign-in (only with `DEMO_AUTH=1`) |

Pages read through the same services as the API, and every button calls the JSON API, so there is one code path for writes.

## Frontend and design system

The app's pages are server-rendered Jinja templates. Their look comes from the v0 design in `frontend_design/` (a Next.js landing page and sign-in placeholder), ported into this app rather than run beside it.

| Piece | Where | Notes |
|---|---|---|
| Design tokens and every component style | `app/static/styles.css` | The v0 palette (ink, cream, paper, butter, coral, mint, cobalt), Anton display type over Inter, pill controls, ink-outlined cards, status badges, stepper, dialog, toast, motion. Event pages add a little in `app/static/events.css`. |
| App shell | `app/templates/base.html`, `_head.html`, `_menu.html` | Sticky top bar, section links, sub-navigation and the glass menu. |
| Navigation model | `navigation()` in `app/web/templating.py` | One list of sections and links with the roles that may see each. Add a module's pages here. |
| Landing page | `app/templates/landing.html`, route `/` | The sliced title, stickers, ticker and feature grid from the design. Tools that exist here link to them; the others say "Coming soon". |
| Sign-in | `/login` → `app/templates/dev_login.html` | `/login` is the stable entry point. Today it leads to the demo sign-in; real sign-in should take this route over. |
| Home | `app/templates/home.html`, route `/home` (where sign-in lands) | Live figures per person from the event, task, expense, ledger and merchandise services. |
| Interaction | `app/static/app.js` | In-page confirm and reason dialogs (no browser pop-ups), toasts, menu, scroll reveals, hero parallax, magnetic buttons. All motion is switched off for `prefers-reduced-motion`. |
| Brand assets | `app/static/brand/` | Hero image and stickers from the design, converted to WebP. |

Not ported: the design's live WebGL ribbon scene (three.js). The landing page uses the design's own still image of that scene, with pointer parallax. Fonts load from Google Fonts and fall back to Impact and the system font offline.

## Seed data

```bash
make seed
```

Creates 1 president, 1 treasurer, 2 event leads, 5 volunteers and 10 members; 3 products with 7 variants; 10 orders (paid, packed, delivered, refunded, failed, unpaid); 8 expense claims across every state; and ledger entries for dues, tickets, a sponsorship, direct expenses, reimbursements and merchandise. Freshers' Night makes a profit and Tech Fest Workshop makes a loss.

`--reset` drops every table first and refuses to run against a database that is not on localhost unless `--force` is given.

## Demo script

After seeding, the balance is ₹15,140.00 and the Hoodie in M has 10 in stock.

1. Sign in as **Aditi Sharma** (volunteer) → **Submit expense**. Download the ₹850 sample receipt from that page and upload it.
2. **Read receipt**: vendor, ₹850.00 and 03 Oct 2026 are filled in. Choose event **Bake Sale**, add a title and description, submit.
3. Switch user to **Sneha Iyer** (event lead) → **Approvals** → **Approve**. ₹850 is under the limit, so the claim is approved.
4. Switch to **Rohan Mehta** (treasurer) → **Approvals** → **Mark as paid**.
5. **Dashboard**: balance is now ₹14,290.00, and the reimbursement is in the ledger.
6. Switch to **Ishaan Gupta** (member) → **Store** → **Club Hoodie** → size M → **Buy now** → **Pay ₹799.00 with Stripe**. On Stripe's page use card **4242 4242 4242 4242**. (With the mock provider the button is **Pay ₹799.00 (mock success)**.)
7. You land on the order confirmation, marked paid. Refresh it: nothing changes. The product page shows 9 left.
8. As the treasurer, the **Ledger** shows the merchandise income entry for that order.
9. **Dashboard**: balance ₹15,089.00, with Bake Sale in the event profit and loss table.

For two-level approval, submit the ₹2,500 sample receipt against **Tech Fest Workshop**: Karthik Nair (lead) approves first, and the volunteer's page shows "First approval by Karthik Nair · awaiting final approval from Rohan Mehta". Then Rohan Mehta or Ananya Rao gives final approval. To show the control, have Rohan approve first instead: he then has no approve button, and Ananya must give the final approval.

For a multi-item receipt, use the third sample on the submit page (six items, tax lines, total ₹968.00).

To watch two users at once, open the second one in a private window: the demo sign-in is one cookie per browser, so "Switch user" changes every tab.

## Integrating other modules

**Recording income** (dues, tickets): call the ledger service inside your own transaction. It flushes but does not commit.

```python
from app.services import ledger_service

await ledger_service.record_income(
    db, club_id=club_id, source_module="event_ticket", amount=Decimal("200"),
    created_by=buyer_id, payer_id=buyer_id, event_id=event_id,
    reference_id=ticket.id, reference_type="ticket",
    idempotency_key=f"{payment_intent_id}:income",
)
```

| Function | Use |
|---|---|
| `record_income(...)` | Dues, ticket sales, merchandise, fundraisers (credit) |
| `record_expense(...)` | Approved reimbursements and direct expenses (debit) |
| `record_refund(...)` | Cancelled or refunded orders and tickets (debit) |
| `record_adjustment(...)` | The only way to correct a posted entry |

**Current user:** `app.core.dependencies.get_current_user` returns `CurrentUser(id, club_id, role, is_active_member, full_name, email)`. Roles are `president`, `treasurer`, `event_lead`, `volunteer`, `member`. Use `require_roles(...)` to restrict a route.

**Replacing the placeholders:** all reads of `users`, `clubs` and `events` go through `app/services/shared_directory.py`. Re-point those six functions at the real tables, drop `migrations/000_shared_placeholders.sql` and `app/models/shared.py`, and remove `app/web/dev_auth.py`.

## Known limitations

- **Sign-in is a demo stand-in.** `/dev/login` lets anyone act as any user. It exists only while `DEMO_AUTH=1`.
- **Payments are mocked** until a Stripe test key is set. Automated tests stub Stripe's network calls; see the note at the end of this list for what has been run against Stripe itself.
- **Supabase is untested.** Database and Storage were developed against local Postgres and local disk; the Supabase paths are config switches that have not been exercised.
- **Locally stored receipts are served without a sign-in check** from `/uploads`, under unguessable names. On Supabase they would be in a public bucket; signed URLs would be the next step.
- **PDF receipts are stored but not read by OCR.** The fields must be typed in.
- **OCR rules are tuned for printed English receipts.** Handwritten, crumpled or badly lit receipts will need manual entry, and individual item lines can still be misread on small images.
- **The store page orders one product variant at a time.** The API (`POST /merch/orders`) and every order page handle several items per order; there is no cart in the review UI.
- **An officer's own large claim needs a third person.** With one treasurer and one president, a ₹1,000+ claim submitted by either of them can only be approved if it is linked to an event whose lead gives the first approval. Otherwise the first approval is refused with `no_final_approver`.
- **Claims approved before the two-person rule are not rewritten.** To list any large claim that reached approval with a single approver: `SELECT id, title, amount, status FROM expense_claims WHERE status IN ('approved','paid') AND first_approved_by IS NULL AND amount >= 1000;`
- **Refunds call the provider before the database commit.** If the commit then failed, a refund could be issued without being recorded. The next verification or webhook would record it; Stripe's idempotency key stops a second refund.
- **Partial refunds made in the Stripe Dashboard are not recorded automatically.** Full refunds are.
- **Mock checkout sessions live in memory** and are forgotten on restart; the order simply gets a new session.
- **Stock is not reserved at order time,** so a buyer can reach payment for an item that sells out first. They are refunded automatically.
- **No pagination on claims and orders lists,** and no CSRF tokens on the review pages (the session cookie is `SameSite=Lax`).
- **`app/models/payment.py` from the planned layout does not exist.** Payments store their ids on `merch_orders`, webhook deliveries in `payment_events` and their history in `audit_logs`.
