-- LOCAL SUPABASE RLS runtime tests only. Before running, create four disposable
-- Auth users in local Supabase, then pass their UUIDs with psql -v. Do not use
-- production IDs. This script seeds public fixtures as DB owner, switches to
-- authenticated with simulated local JWT claims, asserts isolation, then rolls back.
-- Example: psql -v auth_a=... -v auth_b=... -v auth_c=... -v auth_unmapped=... -f this-file
\set ON_ERROR_STOP on
BEGIN;

CREATE TEMP TABLE membercore_auth_fixture_check (ok boolean CHECK (ok)) ON COMMIT DROP;
INSERT INTO membercore_auth_fixture_check (ok)
SELECT count(*) = 4 FROM auth.users
WHERE id IN (:'auth_a'::uuid, :'auth_b'::uuid, :'auth_c'::uuid, :'auth_unmapped'::uuid);
DROP TABLE membercore_auth_fixture_check;

INSERT INTO public.clubs (id, name) VALUES
 ('c1000000-0000-4000-8000-000000000001', 'RLS test club one'),
 ('c2000000-0000-4000-8000-000000000002', 'RLS test club two');
INSERT INTO public.users (id, club_id, full_name, email, role, is_active_member, auth_user_id) VALUES
 ('a1000000-0000-4000-8000-000000000001', 'c1000000-0000-4000-8000-000000000001', 'RLS A', 'rls-a@example.invalid', 'member', false, :'auth_a'::uuid),
 ('a2000000-0000-4000-8000-000000000002', 'c1000000-0000-4000-8000-000000000001', 'RLS B', 'rls-b@example.invalid', 'member', false, :'auth_b'::uuid),
 ('a3000000-0000-4000-8000-000000000003', 'c2000000-0000-4000-8000-000000000002', 'RLS C', 'rls-c@example.invalid', 'member', false, :'auth_c'::uuid);
INSERT INTO public.membership_plans (id, club_id, name, price_paise, validity_days, ticket_discount_bps) VALUES
 ('d1000000-0000-4000-8000-000000000001', 'c1000000-0000-4000-8000-000000000001', 'RLS plan one', 10000, 365, 500),
 ('d2000000-0000-4000-8000-000000000002', 'c2000000-0000-4000-8000-000000000002', 'RLS plan two', 10000, 365, 500);
INSERT INTO public.memberships
 (id, user_id, club_id, plan_id, status, starts_at, expires_at, discount_bps_snapshot) VALUES
 ('e1000000-0000-4000-8000-000000000001', 'a1000000-0000-4000-8000-000000000001', 'c1000000-0000-4000-8000-000000000001', 'd1000000-0000-4000-8000-000000000001', 'pending', now(), now() + interval '30 days', 500),
 ('e2000000-0000-4000-8000-000000000002', 'a2000000-0000-4000-8000-000000000002', 'c1000000-0000-4000-8000-000000000001', 'd1000000-0000-4000-8000-000000000001', 'pending', now(), now() + interval '30 days', 500),
 ('e3000000-0000-4000-8000-000000000003', 'a3000000-0000-4000-8000-000000000003', 'c2000000-0000-4000-8000-000000000002', 'd2000000-0000-4000-8000-000000000002', 'pending', now(), now() + interval '30 days', 500);

SET LOCAL ROLE authenticated;

-- User A sees only own membership, not another user in the same club or any
-- membership in another club. Active plans are club-visible by the proposal.
SELECT set_config('request.jwt.claim.sub', :'auth_a', true);
SELECT set_config('request.jwt.claims', format('{"sub":"%s","role":"authenticated"}', :'auth_a'), true);
DO $check_user_a$
BEGIN
  IF (SELECT count(*) FROM public.memberships) <> 1
     OR NOT EXISTS (SELECT 1 FROM public.memberships WHERE id='e1000000-0000-4000-8000-000000000001')
     OR EXISTS (SELECT 1 FROM public.memberships WHERE id IN
       ('e2000000-0000-4000-8000-000000000002','e3000000-0000-4000-8000-000000000003')) THEN
    RAISE EXCEPTION 'RLS leaked another member or another club to user A';
  END IF;
  IF (SELECT count(*) FROM public.membership_plans) <> 1 THEN
    RAISE EXCEPTION 'Plan policy did not remain scoped to user A club';
  END IF;
  BEGIN
    INSERT INTO public.memberships
      (user_id, club_id, plan_id, status, starts_at, expires_at, discount_bps_snapshot)
    VALUES ('a1000000-0000-4000-8000-000000000001', 'c1000000-0000-4000-8000-000000000001',
      'd1000000-0000-4000-8000-000000000001', 'pending', now(), now()+interval '1 day', 0);
    RAISE EXCEPTION 'Expected browser membership INSERT to be denied';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    UPDATE public.memberships SET status='cancelled';
    RAISE EXCEPTION 'Expected browser membership UPDATE to be denied';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  BEGIN
    DELETE FROM public.memberships;
    RAISE EXCEPTION 'Expected browser membership DELETE to be denied';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
END
$check_user_a$;

-- A different club member sees only that club's active plans and own membership.
SELECT set_config('request.jwt.claim.sub', :'auth_c', true);
SELECT set_config('request.jwt.claims', format('{"sub":"%s","role":"authenticated"}', :'auth_c'), true);
DO $check_user_c$
BEGIN
  IF (SELECT count(*) FROM public.memberships) <> 1
     OR NOT EXISTS (SELECT 1 FROM public.memberships WHERE id='e3000000-0000-4000-8000-000000000003')
     OR (SELECT count(*) FROM public.membership_plans) <> 1
     OR NOT EXISTS (SELECT 1 FROM public.membership_plans WHERE id='d2000000-0000-4000-8000-000000000002') THEN
    RAISE EXCEPTION 'RLS leaked data across clubs to user C';
  END IF;
END
$check_user_c$;

-- Authenticated but unmapped identity has no membership or plan access.
SELECT set_config('request.jwt.claim.sub', :'auth_unmapped', true);
SELECT set_config('request.jwt.claims', format('{"sub":"%s","role":"authenticated"}', :'auth_unmapped'), true);
DO $check_unmapped$
BEGIN
  IF EXISTS (SELECT 1 FROM public.memberships)
     OR EXISTS (SELECT 1 FROM public.membership_plans) THEN
    RAISE EXCEPTION 'Unmapped Auth user received application membership access';
  END IF;
END
$check_unmapped$;

ROLLBACK;
