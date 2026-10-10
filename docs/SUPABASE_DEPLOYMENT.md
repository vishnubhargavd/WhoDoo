# WhoDoo: Supabase & External Provider Production Deployment Guide

This guide details the exact environment variables, architectural flow, and security configurations required to run WhoDoo with genuine Supabase authentication and external cloud services, transitioning away from local demo authentication.

---

## 1. Authentication Architecture

### 1.1 Supabase SSR Flow (Next.js Frontend)
1. **User Sign In / Sign Up**:
   - The user visits `/login` or `/signup` rendered by the Next.js App Router (`web/src/app/login/page.tsx`).
   - The client interacts with Supabase using `@supabase/ssr` with `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`.
   - On signup, a PKCE confirmation link is dispatched to the user's email redirecting to `/auth/callback?next=/account`.
   - The callback route (`web/src/app/auth/callback/route.ts`) exchanges the code for a verified session and sets secure, HTTP-only server session cookies.

2. **Server-Side API Forwarding**:
   - Next.js Server Components and Server Actions call `getBackend()` in `web/src/lib/backend.ts`.
   - `getBackend()` extracts the verified Supabase access token via `verifiedAccessToken(supabase.auth)` (`web/src/lib/backend-contract.ts`).
   - Authenticated HTTP requests are forwarded to the FastAPI backend with:
     ```http
     Authorization: Bearer <SUPABASE_JWT_ACCESS_TOKEN>
     ```

### 1.2 FastAPI ES256 / JWKS Validation & Application User Resolution
1. **Token Verification (`app/core/supabase_auth.py`)**:
   - FastAPI in `AUTH_MODE=supabase` does **NOT** use a shared symmetric secret (`SUPABASE_JWT_SECRET` is explicitly forbidden).
   - The backend retrieves the project's public keys from `<SUPABASE_URL>/auth/v1/.well-known/jwks.json`.
   - The token must be an **ES256** token signed with an EC P-256 public key whose `kid` matches a valid key in the JWKS.
   - The token's `iss` must match `<SUPABASE_URL>/auth/v1` exactly and `aud` must be `"authenticated"`.

2. **Application Identity Resolution (`app/core/dependencies.py` & `app/services/shared_directory.py`)**:
   - The verified token yields the Supabase user ID (`sub`), which is an immutable UUID.
   - The user's club isolation, role (`president`, `treasurer`, `event_lead`, `member`), and permissions are **NEVER** trusted from client inputs or token claims.
   - The application looks up `public.users` where `users.auth_user_id = sub`.
   - If no record is found, the system fails closed with HTTP 401 `account_not_linked`.

### 1.3 Strict Production Isolation of Demo Auth
- When `AUTH_MODE=supabase`, `DEMO_AUTH` **MUST** be `0`.
- In `app/core/config.py`:
  - `if self.demo_auth and not self.database_is_local: raise RuntimeError(...)`
  - `if self.auth_mode == "supabase" and self.demo_auth: raise RuntimeError(...)`
- In `app/main.py`: `dev_auth.router` is never mounted when `settings.demo_auth` is False.
- In `app/web/dev_auth.py`: `/dev/login` endpoints reject any request with HTTP 404 if `not (demo_auth and database_is_local)`.

---

## 2. Environment Variables Specification

### 2.1 Backend FastAPI (`.env`)
| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | Local PG | PostgreSQL connection string. When pointing to remote Supabase DB, use pooled port (e.g. 6543) or direct port (5432). |
| `DB_DISABLE_STATEMENT_CACHE` | Yes (for pooler) | `0` | Set to `1` when using Supabase transaction pooler (port 6543) with PgBouncer. |
| `AUTH_MODE` | Yes | `local` | Set to `supabase` for production. |
| `DEMO_AUTH` | Yes | `0` | Must be `0` in production. |
| `SUPABASE_URL` | Yes | `""` | The Supabase project HTTPS URL, e.g. `https://<project-ref>.supabase.co`. |
| `SUPABASE_SERVICE_KEY` | Optional | `""` | Used only for server-side private Supabase storage bucket access (receipts). Never expose to clients. |
| `SUPABASE_STORAGE_BUCKET` | Optional | `receipts` | Name of the private Supabase Storage bucket for expense receipts. |
| `UPLOAD_DIR` | No | `uploads` | Local disk fallback for uploads when Supabase Storage is not configured. |
| `PAYMENT_PROVIDER` | No | `auto` | `stripe`, `mock`, or `auto`. (UPI / Razorpay integration managed separately by teammate). |
| `STRIPE_SECRET_KEY` | Optional | `""` | Test secret key (`sk_test_...`) for Stripe checkout if enabled. |
| `APP_BASE_URL` | Yes | `http://127.0.0.1:8000` | Fully qualified base URL for API callbacks. |
| `SMTP_HOST` | Optional | `""` | SMTP server host for transactional ticket and announcement emails. |
| `SMTP_PORT` | Optional | `587` | SMTP port (typically 587 for TLS or 465 for SSL). |
| `SMTP_USER` | Optional | `""` | SMTP username. |
| `SMTP_PASS` | Optional | `""` | SMTP password. |
| `SMTP_FROM` | Optional | `tickets@whodoo.org` | From address for transactional emails. |

### 2.2 Frontend Next.js (`web/.env.local`)
| Variable | Required | Description |
|---|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Yes | The Supabase project HTTPS URL (`https://<project-ref>.supabase.co`). |
| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | Yes | The Supabase public client key (`sb_publishable_...` or anon key). |
| `WHODOO_API_URL` | Yes | Origin of the backend FastAPI service (e.g., `http://localhost:8000/` or `https://api.whodoo.org/`). |

---

## 3. External Provider Readiness & Safe Fallbacks

1. **Transactional Email**:
   - Configured: When `SMTP_HOST`, `SMTP_USER`, and `SMTP_PASS` are provided, transactional emails (tickets, notifications) are sent via TLS SMTP.
   - Non-configured Fallback: When omitted, emails are safely logged to `uploads/outbox/emails.log`. The API reports mode `local_outbox` without crashing or falsely claiming external delivery.

2. **Receipt Storage**:
   - Configured: When `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` are provided, files are stored in the private `receipts` bucket via the Supabase Storage SDK.
   - Non-configured Fallback: Falls back to the server's local filesystem under `uploads/receipts/`.

3. **Payments**:
   - Stripe / Mock provider: Defaults to mock payments when `STRIPE_SECRET_KEY` is omitted or in demo mode.
   - Razorpay / UPI: Handled independently by Claude Code teammate.

---

## 4. Credential-Dependent Blockers for Live Remote Staging

To verify live remote operation (beyond local unit/integration tests):
1. **Supabase Project Credentials**: A live Supabase project reference URL, public client key, and database connection string are required.
2. **Email Delivery (SMTP)**: A valid transactional SMTP relay (e.g., SendGrid, Postmark, AWS SES, or custom SMTP server) is required for genuine inbox delivery.
3. **Receipt Bucket**: A private bucket named `receipts` must be created in the Supabase Storage dashboard if using remote storage.
