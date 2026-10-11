# Supabase Auth bridge (FastAPI)

Owner: Member 2. Status: implemented and unit-tested, **off by default** (`AUTH_MODE=local`).
It has not been run against a real Supabase project. Do not switch it on until migration 030 is
applied and the checks under "Before switching it on" pass.

## What happens on a request

```
Authorization: Bearer <Supabase access token>
  -> header alg must be ES256 and carry a kid          (anything else: 401 invalid_token)
  -> key = JWKS[kid] from <SUPABASE_URL>/auth/v1/.well-known/jwks.json
  -> ES256 signature, iss, aud, exp, iat, nbf checked  (401 invalid_token / token_expired)
  -> sub must be a canonical UUID, role "authenticated", not anonymous
  -> SELECT id FROM users WHERE auth_user_id = sub     (401 account_not_linked)
  -> users row -> CurrentUser(id, club_id, role, is_active_member, ...)
```

Everything after "users row" is the existing contract, so events, tasks, finance, merchandise
and FairSeat routes need no change. `get_purchaser` in `app/api/v1/fairseat.py` already goes
through `get_current_user` and keeps returning a `users` row.

Club, role and membership are **always read from `users`** on every request. Nothing in the token
(`app_metadata`, `user_metadata`, custom claims) is used. Changing a role or removing the link
takes effect on the next request, even for a token that has not expired.

## Modes

| `AUTH_MODE` | Accepts | Demo picker | Use |
|---|---|---|---|
| `local` (default) | HS256 tokens signed with `JWT_SECRET`, `sub` = `users.id`; bearer header or session cookie | Only with `DEMO_AUTH=1` **and** a local `DATABASE_URL` | Development, tests |
| `supabase` | ES256 Supabase access tokens only, bearer header only | Never | Real sign-in |

- The mode comes from configuration only, never from the token. There is no fallback: an
  invalid Supabase token never goes on to the HS256 check, and in local mode a Supabase token
  fails the HS256 check.
- In supabase mode the session cookie is ignored and `/dev/login` is not mounted. The
  server-rendered Jinja pages therefore show their sign-in error. The Next.js app is the client.

### Startup refusals (`Settings.check`)

The app will not start when:

- `SUPABASE_JWT_SECRET` is set. It is the legacy shared HS256 secret, not a verification key, and
  is no longer read. Older setup notes (MEMBER3_FINANCE.md "Moving to Supabase") said to set it.
  Remove it.
- `AUTH_MODE=supabase` and `DEMO_AUTH=1`.
- `AUTH_MODE=supabase` and `SUPABASE_URL` is not a bare `https://` project URL. `http://` is
  allowed only for a loopback host, which is the Supabase CLI's local stack.
- `DEMO_AUTH=1` with a `DATABASE_URL` that is not local. **This is new.** Previously this was
  refused only together with `FAIRSEAT_ENABLED=1`. A demo against the shared Supabase database
  must now use real sign-in.
- `AUTH_MODE=local`, `DEMO_AUTH=0`, and `JWT_SECRET` is the default or shorter than 32 characters.
- `JWT_ALGORITHM` is anything but `HS256`, or a tuning value is out of bounds.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `AUTH_MODE` | `local` | `local` or `supabase` |
| `SUPABASE_URL` | — | Project URL. Issuer = `<url>/auth/v1`, JWKS = `<url>/auth/v1/.well-known/jwks.json` |
| `SUPABASE_JWKS_CACHE_SECONDS` | 600 | 60–3600 |
| `SUPABASE_JWKS_TIMEOUT_SECONDS` | 5 | 1–10 (connect ≤ 3) |
| `SUPABASE_JWT_LEEWAY_SECONDS` | 30 | 0–60, applied to exp / iat / nbf |

No secret is needed for supabase mode: the JWKS is public.

## Verification policy (`app/core/supabase_auth.py`)

- **Algorithm:** only `ES256`. `none`, `HS256` (demo tokens, the legacy Supabase secret, or HMAC
  with the public key as secret), RS256 and everything else are rejected before any key lookup.
- **Key:** the JWKS entry whose `kid` matches. Only public EC P-256 keys with alg ES256 (or no alg)
  and use `sig` (or no use) are kept. Symmetric keys, RSA keys and entries containing private
  material (`d`) are ignored. At most 20 keys and 64 KB are accepted.
- **Claims:** `iss` must exactly equal `<SUPABASE_URL>/auth/v1`, `aud` must be `authenticated`,
  and `exp`, `iat`, `iss`, `aud`, `sub` are all required. `exp` must not have passed, `iat` must
  not be in the future, and `nbf` (if present) must have passed, each ± leeway. `sub` must be a
  lowercase hyphenated UUID, `role` must be `authenticated`, and `is_anonymous: true` is refused.
  Tokens over 8 KB are refused.
- **JWKS cache:** refreshed when older than the cache lifetime. An unknown `kid` forces one refresh,
  at most every 30 s, so random kids cannot flood Supabase. A refresh replaces the whole set, so a
  revoked key stops working at the next refresh. If Supabase is unreachable, keys fetched within
  the last hour keep working; after that, and when nothing is cached, requests get
  `503 auth_unavailable`, never a bypass. Fetches are HTTPS, follow no redirects and are time-bounded.
- **Rotation:** Supabase publishes a standby key before it becomes current, and the cache picks it
  up within the cache lifetime. A key used within 30 s of our last fetch, without having been
  published first, fails for up to 30 s and then works.
- **Logging:** reason codes only (`unknown kid`, `expired`, ...) at INFO. Tokens, signatures and
  library messages are never logged or returned. All API errors use the existing
  `{"error": {"code", "message", "details": null}}` shape.

## Identity mapping (`app/services/shared_directory.py`)

- `get_user_by_auth_user_id` matches `users.auth_user_id` only. There is **no email matching** and
  **no reading `sub` as `users.id`**.
- Migration 030 is not in the shared schema yet, so `auth_user_id` is **not** on the `User` ORM
  model, and existing queries are unaffected. The lookup uses plain SQL.
- Before the first lookup it checks the catalog: `users.auth_user_id` must exist, be `uuid`, and
  have a single-column, non-partial UNIQUE index or constraint. If not, every Supabase sign-in
  gets `503 auth_unavailable`, and the log says to apply migration 030. It re-checks on each
  sign-in until the column appears, so no restart is needed after the migration. More than one
  matching row is also treated as unavailable.
- After migration 030 lands, the ORM model can gain the column in a separate, reviewed change.

## Contract for Ameer: migration 030 and linking

Needed by this bridge:

1. `users.auth_user_id uuid NULL` with a `UNIQUE` constraint. A plain constraint is fine; a partial
   unique index is **not** accepted by the bridge's schema check.
2. Existing `users.id` values stay as they are. Every finance, event, task and FairSeat table
   refers to `users.id`.
3. Linking is done by a trusted, server-side process: an admin action or a provisioning job that
   runs with the service role, or SQL reviewed by the team. **Never** by matching emails
   automatically, and never by an endpoint where a signed-in user chooses which `users` row is
   theirs. The bridge has no linking endpoint on purpose.
4. To block someone immediately, set `auth_user_id = NULL`, or change `role`. Banning the user in
   Supabase Auth stops new tokens, but an already-issued token works until it expires (Supabase
   default: 1 hour). The bridge does not check Supabase sessions.
5. The Supabase-only foreign key to `auth.users(id)`, grants and RLS policies are Ameer's.
   FastAPI connects with the `DATABASE_URL` credentials. If that role owns the tables or bypasses
   RLS, which is typical for a server connection, RLS does not restrict it. Authorization in FastAPI
   stays in the application code. Ameer should confirm which role the API uses. Do not disable RLS for the API's sake.

## Handoff for the Next.js frontend

- Send the **Supabase user access token** (`session.access_token`) as
  `Authorization: Bearer <token>` on every call to `/api/v1/...`. Never send the refresh token,
  the anon/publishable key, or the service-role key. The service-role key must never reach the
  browser.
- Prefer calling FastAPI from server code (route handlers or server actions) with the session from
  `@supabase/ssr`. Use `supabase.auth.getUser()` / `getClaims()` server-side rather than trusting
  `getSession()` blindly. If the browser calls FastAPI directly, add CORS for the frontend origin
  in a separate, reviewed change; there is no CORS middleware today.
- Do **not** send club, role or membership. The API ignores them and derives everything from the
  linked `users` row. Use the API's own responses for what to display.
- Responses to handle:

| Status | `error.code` | Frontend action |
|---|---|---|
| 401 | `not_authenticated` | No token was sent: sign in |
| 401 | `token_expired` | Refresh the Supabase session once and retry; if that fails, sign in again |
| 401 | `invalid_token` | Do not retry; sign out and sign in again |
| 401 | `account_not_linked` | Signed in to Supabase but no WhoDoo account: show "ask your club admin to link your account"; do not loop to sign-in |
| 403 | `forbidden` | Signed in, but the role does not allow this |
| 503 | `auth_unavailable` | Temporary: keep the session, show a retry message |

## Before switching it on

1. Ameer applies migration 030 to the target database. Links are created through the approved
   provisioning path.
2. The Supabase project uses **asymmetric (ECC P-256) JWT signing keys**. A project still on the
   legacy shared secret publishes an empty JWKS, and the bridge then answers `503 auth_unavailable`
   to every request. Do not change the signing keys from this repository.
3. Set `AUTH_MODE=supabase`, `SUPABASE_URL`, `DEMO_AUTH=0`, and remove `SUPABASE_JWT_SECRET`.
4. Test end to end with one linked test account and one unlinked test account on an approved
   database, before any claim that live sign-in works.

## Tests

`tests/test_auth_supabase.py`. Keys are generated per test, the JWKS is faked (or served through
`httpx.MockTransport`), and no network or real Supabase token is involved. The API-level tests add
`auth_user_id` to the disposable `whodoo_test` database for the duration of each test and drop it
afterwards. They need the same local Postgres as the rest of the suite (`make test`).
