-- PROPOSAL TEST ONLY — psql against a disposable DB after the approved shared
-- migration chain and portable 030/031. It inserts fixtures and always rolls back.
-- Never run against live Supabase or a shared application database.
\set ON_ERROR_STOP on
BEGIN;
DO $assertions$
DECLARE
  u1 uuid := 'a1111111-1111-4111-8111-111111111111';
  u2 uuid := 'a2222222-2222-4222-8222-222222222222';
  c1 uuid := 'c1111111-1111-4111-8111-111111111111';
  c2 uuid := 'c2222222-2222-4222-8222-222222222222';
  auth1 uuid := 'b1111111-1111-4111-8111-111111111111';
  auth2 uuid := 'b2222222-2222-4222-8222-222222222222';
  p1 uuid := 'd1111111-1111-4111-8111-111111111111';
  p2 uuid := 'd2222222-2222-4222-8222-222222222222';
  m1 uuid := 'e1111111-1111-4111-8111-111111111111';
  m2 uuid := 'e2222222-2222-4222-8222-222222222222';
  r1 uuid := 'f1111111-1111-4111-8111-111111111111';
  r2 uuid := 'f2222222-2222-4222-8222-222222222222';
  r3 uuid := 'f3333333-3333-4333-8333-333333333333';
  d1 uuid := 'a3333333-3333-4333-8333-333333333333';
  d2 uuid := 'a6666666-6666-4666-8666-666666666666';
  failed_event uuid := 'a4444444-4444-4444-8444-444444444444';
  paid_event uuid := 'a5555555-5555-4555-8555-555555555555';
  paid_event_2 uuid := 'a7777777-7777-4777-8777-777777777777';
  v_rows integer;
BEGIN
  IF to_regclass('public.clubs') IS NULL OR to_regclass('public.users') IS NULL
     OR to_regclass('public.membership_plans') IS NULL OR to_regclass('public.memberships') IS NULL
     OR to_regclass('public.membership_renewals') IS NULL OR to_regclass('public.membership_dues') IS NULL
     OR to_regclass('public.membership_dues_reconciliations') IS NULL
     OR to_regclass('public.membership_activation_grants') IS NULL
     OR to_regclass('public.membercore_audit_events') IS NULL THEN
    RAISE EXCEPTION 'Apply the approved shared chain and proposed 030/031 first';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name='users' AND column_name='is_active_member'
        AND data_type='boolean') THEN
    RAISE EXCEPTION 'Existing users.is_active_member compatibility column is missing';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname='users_auth_user_id_uq')
     OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.memberships'::regclass
       AND conname='memberships_user_club_uq' AND contype='u') THEN
    RAISE EXCEPTION 'Identity uniqueness or one-membership-per-user/club constraint missing';
  END IF;

  INSERT INTO public.clubs (id, name) VALUES (c1, 'MemberCore test A'), (c2, 'MemberCore test B');
  INSERT INTO public.users (id, club_id, full_name, email, role, is_active_member)
    VALUES (u1, c1, 'MemberCore One', 'membercore-one@example.invalid', 'member', true),
           (u2, c2, 'MemberCore Two', 'membercore-two@example.invalid', 'member', false);
  SELECT count(*) INTO v_rows FROM public.users WHERE id IN (u1, u2) AND auth_user_id IS NULL;
  IF v_rows <> 2 THEN RAISE EXCEPTION 'Multiple unmapped users were not allowed'; END IF;
  UPDATE public.users SET auth_user_id=auth1 WHERE id=u1;
  UPDATE public.users SET auth_user_id=auth2 WHERE id=u2;
  BEGIN
    UPDATE public.users SET auth_user_id=auth1 WHERE id=u2;
    RAISE EXCEPTION 'Expected duplicate Auth mapping to fail';
  EXCEPTION WHEN unique_violation THEN NULL;
  END;
  IF (SELECT id FROM public.users WHERE auth_user_id=auth1) <> u1
     OR NOT EXISTS (SELECT 1 FROM public.users WHERE id=u1 AND is_active_member=true)
     OR NOT EXISTS (SELECT 1 FROM public.users WHERE id=u2 AND is_active_member=false) THEN
    RAISE EXCEPTION 'Existing application IDs or is_active_member values changed';
  END IF;

  INSERT INTO public.membership_plans
    (id, club_id, name, price_paise, validity_days, ticket_discount_bps)
  VALUES (p1, c1, 'Test standard', 12500, 365, 1250),
         (p2, c2, 'Free max discount', 0, 30, 10000),
         ('d3333333-3333-4333-8333-333333333333', c1, 'TrustLedger maximum', 999999999999, 365, 0);
  BEGIN
    INSERT INTO public.membership_plans (club_id, name, price_paise, validity_days)
    VALUES (c1, 'Over TrustLedger maximum', 1000000000000, 30);
    RAISE EXCEPTION 'Expected price above numeric(12,2) rupee range to fail';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  BEGIN
    INSERT INTO public.membership_plans (club_id, name, price_paise, validity_days, ticket_discount_bps)
    VALUES (c1, 'Invalid discount', 1, 30, 10001);
    RAISE EXCEPTION 'Expected discount above 10000 bps to fail';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  BEGIN
    INSERT INTO public.membership_plans (club_id, name, price_paise, validity_days)
    VALUES (c1, 'Negative paise', -1, 30);
    RAISE EXCEPTION 'Expected negative paise to fail';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  -- New enrollment remains pending until settlement/waiver proof exists.
  INSERT INTO public.memberships
    (id, user_id, club_id, plan_id, status, starts_at, expires_at, discount_bps_snapshot)
  VALUES (m1, u1, c1, p1, 'pending', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', 1250);
  BEGIN
    INSERT INTO public.memberships
      (user_id, club_id, plan_id, status, starts_at, expires_at, discount_bps_snapshot)
    VALUES (u1, c1, p1, 'pending', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', 1250);
    RAISE EXCEPTION 'Expected duplicate current membership to fail';
  EXCEPTION WHEN unique_violation THEN NULL;
  END;
  BEGIN
    INSERT INTO public.memberships
      (user_id, club_id, plan_id, status, starts_at, expires_at, discount_bps_snapshot)
    VALUES (u1, c2, p2, 'pending', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', 10000);
    RAISE EXCEPTION 'Expected cross-club membership FK to fail';
  EXCEPTION WHEN foreign_key_violation THEN NULL;
  END;
  BEGIN
    INSERT INTO public.memberships
      (user_id, club_id, plan_id, status, starts_at, expires_at, discount_bps_snapshot)
    VALUES (u1, c1, p1, 'pending', '2027-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 1250);
    RAISE EXCEPTION 'Expected reversed dates to fail';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  BEGIN
    INSERT INTO public.memberships
      (user_id, club_id, plan_id, status, starts_at, expires_at, discount_bps_snapshot)
    VALUES ('a9999999-9999-4999-8999-999999999999', c1, p1, 'pending',
      '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', 1250);
    RAISE EXCEPTION 'Expected invalid application-user FK to fail';
  EXCEPTION WHEN foreign_key_violation THEN NULL;
  END;

  BEGIN
    INSERT INTO public.membership_renewals
      (membership_id, club_id, renewal_number, request_id, plan_id,
       new_starts_at, new_expires_at, discount_bps_snapshot)
    VALUES (m1, c1, 99, 'f1212121-1212-4121-8121-121212121212', p1,
      '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', 5000);
    RAISE EXCEPTION 'Expected plan/renewal snapshot mismatch to fail';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  INSERT INTO public.membership_renewals
    (id, membership_id, club_id, renewal_number, request_id, plan_id,
     previous_starts_at, previous_expires_at, new_starts_at, new_expires_at, discount_bps_snapshot)
  VALUES (r1, m1, c1, 1, 'f3333333-3333-4333-8333-333333333333', p1,
    NULL, NULL, '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', 1250);
  BEGIN
    INSERT INTO public.membership_renewals
      (membership_id, club_id, renewal_number, request_id, plan_id, new_starts_at, new_expires_at, discount_bps_snapshot)
    VALUES (m1, c1, 1, 'f4444444-4444-4444-8444-444444444444', p1,
      '2027-01-01T00:00:00Z', '2028-01-01T00:00:00Z', 1250);
    RAISE EXCEPTION 'Expected duplicate renewal sequence to fail';
  EXCEPTION WHEN unique_violation THEN NULL;
  END;
  BEGIN
    UPDATE public.membership_renewals SET discount_bps_snapshot=5000 WHERE id=r1;
    RAISE EXCEPTION 'Expected renewal history mutation to fail';
  EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
  END;

  INSERT INTO public.membership_dues
    (id, membership_id, club_id, renewal_id, amount_paise, idempotency_key)
  VALUES (d1, m1, c1, r1, 12500, 'f5555555-5555-4555-8555-555555555555');
  BEGIN
    INSERT INTO public.membership_dues
      (membership_id, club_id, renewal_id, amount_paise, idempotency_key)
    VALUES (m1, c1, r1, 12500, 'f6666666-6666-4666-8666-666666666666');
    RAISE EXCEPTION 'Expected one dues obligation per renewal';
  EXCEPTION WHEN unique_violation THEN NULL;
  END;
  BEGIN
    UPDATE public.membership_dues SET amount_paise=0 WHERE id=d1;
    RAISE EXCEPTION 'Expected dues history mutation to fail';
  EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
  END;

  -- A failed provider result is not activation proof. Duplicate provider event
  -- references are idempotently rejected by the database unique index.
  INSERT INTO public.membership_dues_reconciliations
    (id, dues_id, club_id, source, provider, provider_event_id, request_id, outcome, verified_at)
  VALUES (failed_event, d1, c1, 'provider_webhook', 'test-provider', 'evt-failed',
    'f7777777-7777-4777-8777-777777777777', 'failed', now());
  BEGIN
    INSERT INTO public.membership_dues_reconciliations
      (dues_id, club_id, source, provider, provider_event_id, request_id, outcome, verified_at)
    VALUES (d1, c1, 'provider_webhook', 'test-provider', 'evt-failed',
      'f8888888-8888-4888-8888-888888888888', 'failed', now());
    RAISE EXCEPTION 'Expected duplicate provider event to fail';
  EXCEPTION WHEN unique_violation THEN NULL;
  END;
  BEGIN
    UPDATE public.memberships SET status='active' WHERE id=m1;
    RAISE EXCEPTION 'Expected pending membership activation without proof to fail';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  BEGIN
    INSERT INTO public.membership_activation_grants
      (membership_id, club_id, renewal_id, source, dues_id, reconciliation_id, request_id)
    VALUES (m1, c1, r1, 'paid_settlement', d1, failed_event,
      'f9999999-9999-4999-8999-999999999999');
    RAISE EXCEPTION 'Expected failed payment to be rejected as activation proof';
  EXCEPTION WHEN check_violation THEN NULL;
  END;

  INSERT INTO public.membership_dues_reconciliations
    (id, dues_id, club_id, source, provider, provider_event_id, request_id, outcome, verified_at)
  VALUES (paid_event, d1, c1, 'provider_webhook', 'test-provider', 'evt-paid',
    'a7777777-7777-4777-8777-777777777777', 'paid', now());
  INSERT INTO public.membership_activation_grants
    (membership_id, club_id, renewal_id, source, dues_id, reconciliation_id, request_id)
  VALUES (m1, c1, r1, 'paid_settlement', d1, paid_event,
    'a8888888-8888-4888-8888-888888888888');
  UPDATE public.memberships SET status='active' WHERE id=m1;
  IF NOT EXISTS (SELECT 1 FROM public.membercore_audit_events
      WHERE entity_id=m1 AND action='paid_membership_activated') THEN
    RAISE EXCEPTION 'Paid activation audit row missing';
  END IF;

  -- Purchased terms survive plan edits; a later renewal captures the new rate.
  UPDATE public.membership_plans SET ticket_discount_bps=5000 WHERE id=p1;
  IF (SELECT discount_bps_snapshot FROM public.memberships WHERE id=m1) <> 1250
     OR (SELECT discount_bps_snapshot FROM public.membership_renewals WHERE id=r1) <> 1250 THEN
    RAISE EXCEPTION 'Plan edit retroactively changed purchased discount terms';
  END IF;
  INSERT INTO public.membership_renewals
    (id, membership_id, club_id, renewal_number, request_id, plan_id,
     previous_starts_at, previous_expires_at, new_starts_at, new_expires_at, discount_bps_snapshot)
  VALUES (r3, m1, c1, 2, 'f4444444-4444-4444-8444-444444444444', p1,
    '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z',
    '2027-01-01T00:00:00Z', '2028-01-01T00:00:00Z', 5000);
  INSERT INTO public.membership_dues
    (id, membership_id, club_id, renewal_id, amount_paise, idempotency_key)
  VALUES (d2, m1, c1, r3, 12500, 'f5555555-5555-4555-8555-555555555556');
  INSERT INTO public.membership_dues_reconciliations
    (id, dues_id, club_id, source, provider, provider_event_id, request_id, outcome, verified_at)
  VALUES (paid_event_2, d2, c1, 'provider_webhook', 'test-provider', 'evt-paid-2',
    'a8888888-8888-4888-8888-888888888889', 'paid', now());
  INSERT INTO public.membership_activation_grants
    (membership_id, club_id, renewal_id, source, dues_id, reconciliation_id, request_id)
  VALUES (m1, c1, r3, 'paid_settlement', d2, paid_event_2,
    'a9999999-9999-4999-8999-999999999997');
  UPDATE public.memberships
  SET starts_at='2027-01-01T00:00:00Z', expires_at='2028-01-01T00:00:00Z',
      discount_bps_snapshot=5000, status='active'
  WHERE id=m1;
  IF (SELECT discount_bps_snapshot FROM public.membership_renewals WHERE id=r3) <> 5000
     OR (SELECT discount_bps_snapshot FROM public.memberships WHERE id=m1) <> 5000 THEN
    RAISE EXCEPTION 'New renewal did not capture the updated plan discount';
  END IF;
  IF NOT ('2026-06-01T00:00:00Z'::timestamptz >= '2026-06-01T00:00:00Z'::timestamptz
      AND '2026-06-01T00:00:00Z'::timestamptz < '2026-07-01T00:00:00Z'::timestamptz)
     OR ('2026-07-01T00:00:00Z'::timestamptz >= '2026-06-01T00:00:00Z'::timestamptz
      AND '2026-07-01T00:00:00Z'::timestamptz < '2026-07-01T00:00:00Z'::timestamptz) THEN
    RAISE EXCEPTION 'Expected eligibility interval [starts_at, expires_at)';
  END IF;

  BEGIN
    INSERT INTO public.membership_dues_reconciliations
      (dues_id, club_id, source, provider, provider_event_id, request_id, outcome, verified_at)
    VALUES (d1, c1, 'provider_webhook', 'test-provider', 'evt-failed-after-paid',
      'a9999999-9999-4999-8999-999999999998', 'failed', now());
    RAISE EXCEPTION 'Expected failed result after settlement to be rejected';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  INSERT INTO public.membership_dues_reconciliations
    (dues_id, club_id, source, provider, provider_event_id, request_id, outcome, verified_at)
  VALUES (d1, c1, 'provider_webhook', 'test-provider', 'evt-refund-old-term',
    'b1111111-1111-4111-8111-111111111112', 'refunded', now());
  IF (SELECT status FROM public.memberships WHERE id=m1) <> 'active' THEN
    RAISE EXCEPTION 'Refund of prior term revoked a newer current paid term';
  END IF;
  INSERT INTO public.membership_dues_reconciliations
    (dues_id, club_id, source, provider, provider_event_id, request_id, outcome, verified_at)
  VALUES (d2, c1, 'provider_webhook', 'test-provider', 'evt-refund-current',
    'b1111111-1111-4111-8111-111111111113', 'refunded', now());
  IF (SELECT status FROM public.memberships WHERE id=m1) <> 'cancelled'
     OR NOT EXISTS (SELECT 1 FROM public.membercore_audit_events
       WHERE entity_id=m1 AND action='refunded_current_term_cancelled') THEN
    RAISE EXCEPTION 'Refund failed to cancel and audit the current paid term';
  END IF;

  -- Complimentary activation is a distinct, audited waiver and has no fake dues.
  INSERT INTO public.memberships
    (id, user_id, club_id, plan_id, status, starts_at, expires_at, discount_bps_snapshot)
  VALUES (m2, u2, c2, p2, 'pending', '2026-02-01T00:00:00Z', '2026-03-01T00:00:00Z', 10000);
  INSERT INTO public.membership_renewals
    (id, membership_id, club_id, renewal_number, request_id, plan_id,
     previous_starts_at, previous_expires_at, new_starts_at, new_expires_at, discount_bps_snapshot)
  VALUES (r2, m2, c2, 1, 'b2222222-2222-4222-8222-222222222222', p2,
    NULL, NULL, '2026-02-01T00:00:00Z', '2026-03-01T00:00:00Z', 10000);
  INSERT INTO public.membership_activation_grants
    (membership_id, club_id, renewal_id, source, waived_by_user_id, waiver_reason, request_id)
  VALUES (m2, c2, r2, 'complimentary_waiver', u2, 'Approved student hardship waiver',
    'b3333333-3333-4333-8333-333333333333');
  UPDATE public.memberships SET status='active' WHERE id=m2;
  IF NOT EXISTS (SELECT 1 FROM public.membercore_audit_events
       WHERE entity_id=m2 AND action='complimentary_waiver_granted'
         AND after_state->>'waiver_reason'='Approved student hardship waiver')
     OR EXISTS (SELECT 1 FROM public.membership_dues WHERE membership_id=m2) THEN
    RAISE EXCEPTION 'Complimentary activation was not audited separately from payment';
  END IF;
  BEGIN
    INSERT INTO public.membership_activation_grants
      (membership_id, club_id, renewal_id, source, waived_by_user_id, waiver_reason, request_id)
    VALUES (m1, c1, 'b4444444-4444-4444-8444-444444444444',
      'complimentary_waiver', u2, 'Cross-club waiver', 'b5555555-5555-4555-8555-555555555555');
    RAISE EXCEPTION 'Expected invalid/cross-club waiver actor or renewal FK to fail';
  EXCEPTION WHEN foreign_key_violation THEN NULL;
  END;
END
$assertions$;
ROLLBACK;
