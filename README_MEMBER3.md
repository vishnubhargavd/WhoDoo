# WhoDoo — Finance, Reimbursements & Commerce module (Member 3)

WhoDoo is an extracurricular club management system for college students. This is one part of it: a FastAPI backend for the money side of a club, with plain HTML pages for review.

| Module | What it does |
|---|---|
| **TrustLedger** | One immutable ledger for all income, expenses, reimbursements and refunds. Treasurer dashboard, filters, event profit and loss. |
| **Snap-to-Reimburse** | Upload a receipt, OCR reads vendor, amount and date, the volunteer confirms, approvers approve, the treasurer pays. |
| **MerchFlow** | Merchandise catalogue with member pricing, orders, and stock that cannot be oversold. |
| **Payments** | Razorpay-compatible order → verify → webhook flow. Runs on a **Mock Payment (Razorpay-compatible)** provider until Razorpay keys are set. |

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
| `make test` | Run all 64 tests against the `whodoo_test` database |
| `make test-ledger`, `test-expenses`, `test-merch` | Run one module's tests |
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
| `PAYMENT_PROVIDER` | `auto` | `auto` uses Razorpay when both keys are set, otherwise the mock. `mock` forces the mock. |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | — | Razorpay test-mode keys. |

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

The tables follow the brief. Additions, and why:

| Addition | Reason |
|---|---|
| `ledger_entries.idempotency_key` (unique) | A retried webhook or double click cannot post an entry twice, while a refund or adjustment for the same reference still can. Payment-driven keys are `<razorpay_payment_id>:<entry_type>`. |
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
| POST | `/payments/razorpay/create-order` | Opens a provider order for your unpaid merch order. The server sets the amount. |
| POST | `/payments/razorpay/verify` | Verifies the checkout signature, then fulfils the order. |
| POST | `/payments/razorpay/webhook` | `payment.captured`, `order.paid`, `payment.failed`. Authenticated by `X-Razorpay-Signature`. |
| POST | `/payments/mock/simulate` | Mock provider only. Stands in for the Razorpay Checkout popup. |

## Approval workflow

```
submit ──► pending_approval (first) ──approve──► approved ──mark-paid──► paid
                  │   amount ≥ limit                ▲
                  └──approve──► pending_approval (final) ──approve──┘
           reject ──► rejected        request-changes ──► change_requested ──resubmit──► back to first
```

- **Below ₹1,000:** one approval, from the event's lead. The treasurer or president can step in.
- **₹1,000 or more:** the event lead's approval is only the first step; the claim then needs the treasurer or president.
- **A treasurer's or president's approval is always final,** at either stage, so they can approve a large claim directly.
- **The submitter always sees where the claim stands.** Every claim carries a `status_label`, e.g. "Approved by event lead · awaiting final approval from Rohan Mehta", and the claim page shows the steps. Open pages refresh themselves within about five seconds of a change.
- **First approver:** the linked event's lead. With no event, or when the lead is the submitter, it goes to the treasurer, then the president.
- **Nobody decides on or pays out their own claim.** This applies to the treasurer too.
- **Final approval** writes exactly one ledger entry: `reimbursement`, `debit`, `approved`.
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
POST /merch/orders                      order created, stock checked, nothing reserved
POST /payments/razorpay/create-order    provider order opened for the server's amount
   Razorpay Checkout (or mock/simulate) returns order id, payment id, signature
POST /payments/razorpay/verify          signature checked with the key secret
   └─ one transaction: order → paid, stock reduced, merchandise income posted to the ledger
```

- **Stock** is reduced by `UPDATE … WHERE stock_quantity >= quantity`. If two buyers pay for the last item, one succeeds and the other is refunded and gets `409 out_of_stock`.
- **A bad signature** marks the order failed and cancelled; stock is untouched.
- **Retries are safe:** verifying again, or a webhook arriving after verify, changes nothing.
- **Cancelling a paid order** restores stock and posts a `refund` entry; the original income entry is left alone.

### Mock Payment (Razorpay-compatible)

With no Razorpay keys, `MockPaymentProvider` is used. It issues `order_mock_…` and `pay_mock_…` ids and signs them with the same HMAC-SHA256 scheme as Razorpay, so the verify, webhook and ledger code is identical. The checkout page is labelled as mock and offers a success and a failure button. **No real money moves.**

To switch to Razorpay Test Mode, set the three `RAZORPAY_*` variables and restart. No code changes.

## Pages

| Path | Page |
|---|---|
| `/treasurer` | Dashboard: balance, totals, event profit and loss, trend |
| `/treasurer/ledger` | Ledger with filters and manual entry |
| `/expenses`, `/expenses/new`, `/expenses/{id}` | My claims, submit with OCR, claim detail |
| `/expenses/approvals` | Approval queue and claims waiting for payment |
| `/store`, `/store/{product_id}`, `/checkout/{order_id}`, `/orders` | Store, product, payment, my orders |
| `/admin/merch`, `/admin/merch/orders` | Products, stock, order management |
| `/dev/login` | Demo sign-in (only with `DEMO_AUTH=1`) |

Pages read through the same services as the API, and every button calls the JSON API, so there is one code path for writes.

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
6. Switch to **Ishaan Gupta** (member) → **Store** → **Club Hoodie** → size M → **Continue to payment** → **Pay ₹799.00 (mock success)**.
7. The product page shows 9 left.
8. As the treasurer, the **Ledger** shows the merchandise income entry for that order.
9. **Dashboard**: balance ₹15,089.00, with Bake Sale in the event profit and loss table.

For two-level approval, submit the ₹2,500 sample receipt against **Tech Fest Workshop**: Karthik Nair (lead) approves first, and the volunteer's page shows "Approved by event lead · awaiting final approval". Then Rohan Mehta or Ananya Rao gives final approval.

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
    idempotency_key=f"{razorpay_payment_id}:income",
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
- **Payments are mocked** until Razorpay keys are set. The Razorpay provider is written against the SDK but has not been run against Razorpay's servers.
- **Supabase is untested.** Database and Storage were developed against local Postgres and local disk; the Supabase paths are config switches that have not been exercised.
- **Locally stored receipts are served without a sign-in check** from `/uploads`, under unguessable names. On Supabase they would be in a public bucket; signed URLs would be the next step.
- **PDF receipts are stored but not read by OCR.** The fields must be typed in.
- **OCR rules are tuned for printed English receipts.** Handwritten, crumpled or badly lit receipts will need manual entry, and individual item lines can still be misread on small images.
- **The store page orders one product variant at a time.** The API (`POST /merch/orders`) and every order page handle several items per order; there is no cart in the review UI.
- **Approve, reject and pay buttons use the browser's own confirm and prompt boxes.**
- **Refunds call the provider before the database commit.** If the commit then failed, a refund could be issued without being recorded.
- **Stock is not reserved at order time,** so a buyer can reach payment for an item that sells out first. They are refunded automatically.
- **No pagination on claims and orders lists,** and no CSRF tokens on the review pages (the session cookie is `SameSite=Lax`).
- **`app/models/payment.py` from the planned layout does not exist.** Payments store their ids on `merch_orders` and their history in `audit_logs`.
