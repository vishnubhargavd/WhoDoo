-- PROPOSAL ONLY — portable SQL migration 031 for app/db/migrate.py.
-- Keep in migrations/pending/ until approved; run after 030. The runner owns
-- the transaction. No auth schema, Supabase roles, grants, or RLS are assumed.

DO $preflight$
BEGIN
  IF to_regclass('public.clubs') IS NULL OR to_regclass('public.users') IS NULL THEN
    RAISE EXCEPTION '031 requires public.clubs and public.users';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name='users' AND column_name='auth_user_id'
        AND data_type='uuid' AND is_nullable='YES') THEN
    RAISE EXCEPTION '031 requires nullable UUID public.users.auth_user_id from 030';
  END IF;
  IF to_regclass('public.users_id_club_id_uq') IS NULL THEN
    RAISE EXCEPTION '031 requires the users(id, club_id) unique index from 030';
  END IF;
END
$preflight$;

CREATE SCHEMA IF NOT EXISTS membercore_internal;
REVOKE ALL ON SCHEMA membercore_internal FROM PUBLIC;

CREATE TABLE public.membership_plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  club_id uuid NOT NULL REFERENCES public.clubs(id) ON DELETE RESTRICT,
  name text NOT NULL,
  description text,
  price_paise bigint NOT NULL,
  currency text NOT NULL DEFAULT 'INR',
  validity_days integer NOT NULL,
  ticket_discount_bps integer NOT NULL DEFAULT 0,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT membership_plans_name_check CHECK (length(btrim(name)) BETWEEN 1 AND 120),
  CONSTRAINT membership_plans_description_check CHECK (description IS NULL OR length(description) <= 2000),
  -- Keep values representable by the existing TypeScript safe-integer price contract.
  -- numeric(12,2) rupees accepts at most 9,999,999,999.99 = 999,999,999,999 paise.
  CONSTRAINT membership_plans_price_paise_check CHECK (price_paise BETWEEN 0 AND 999999999999),
  CONSTRAINT membership_plans_currency_check CHECK (currency = 'INR'),
  CONSTRAINT membership_plans_validity_days_check CHECK (validity_days BETWEEN 1 AND 3650),
  CONSTRAINT membership_plans_discount_bps_check CHECK (ticket_discount_bps BETWEEN 0 AND 10000),
  CONSTRAINT membership_plans_id_club_uq UNIQUE (id, club_id)
);
CREATE INDEX membership_plans_club_active_idx ON public.membership_plans (club_id, is_active);

CREATE TABLE public.memberships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL,
  club_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  status text NOT NULL,
  starts_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  discount_bps_snapshot integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT memberships_status_check CHECK (status IN ('pending', 'active', 'cancelled', 'expired')),
  CONSTRAINT memberships_validity_check CHECK (
    isfinite(starts_at) AND isfinite(expires_at) AND expires_at > starts_at
  ),
  CONSTRAINT memberships_discount_snapshot_check CHECK (discount_bps_snapshot BETWEEN 0 AND 10000),
  -- A membership is current state; renewals are immutable history rows below.
  CONSTRAINT memberships_user_club_uq UNIQUE (user_id, club_id),
  CONSTRAINT memberships_id_club_uq UNIQUE (id, club_id),
  CONSTRAINT memberships_user_club_fk FOREIGN KEY (user_id, club_id)
    REFERENCES public.users (id, club_id) ON DELETE RESTRICT,
  CONSTRAINT memberships_plan_club_fk FOREIGN KEY (plan_id, club_id)
    REFERENCES public.membership_plans (id, club_id) ON DELETE RESTRICT
);
CREATE INDEX memberships_club_status_expiry_idx ON public.memberships (club_id, status, expires_at);
CREATE INDEX memberships_plan_club_idx ON public.memberships (plan_id, club_id);
-- (user_id, club_id) is already indexed by the unique constraint.

CREATE TABLE public.membership_renewals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  membership_id uuid NOT NULL,
  club_id uuid NOT NULL,
  renewal_number integer NOT NULL,
  request_id uuid NOT NULL,
  plan_id uuid NOT NULL,
  previous_starts_at timestamptz,
  previous_expires_at timestamptz,
  new_starts_at timestamptz NOT NULL,
  new_expires_at timestamptz NOT NULL,
  discount_bps_snapshot integer NOT NULL,
  actor_user_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT membership_renewals_number_check CHECK (renewal_number > 0),
  CONSTRAINT membership_renewals_new_window_check CHECK (
    isfinite(new_starts_at) AND isfinite(new_expires_at) AND new_expires_at > new_starts_at
  ),
  CONSTRAINT membership_renewals_discount_snapshot_check CHECK (discount_bps_snapshot BETWEEN 0 AND 10000),
  CONSTRAINT membership_renewals_previous_window_check CHECK (
    (previous_starts_at IS NULL AND previous_expires_at IS NULL) OR
    (previous_starts_at IS NOT NULL AND previous_expires_at IS NOT NULL
      AND isfinite(previous_starts_at) AND isfinite(previous_expires_at)
      AND previous_expires_at > previous_starts_at)
  ),
  CONSTRAINT membership_renewals_membership_number_uq UNIQUE (membership_id, renewal_number),
  CONSTRAINT membership_renewals_request_uq UNIQUE (membership_id, request_id),
  CONSTRAINT membership_renewals_id_scope_uq UNIQUE (id, membership_id, club_id),
  CONSTRAINT membership_renewals_membership_club_fk FOREIGN KEY (membership_id, club_id)
    REFERENCES public.memberships (id, club_id) ON DELETE RESTRICT,
  CONSTRAINT membership_renewals_plan_club_fk FOREIGN KEY (plan_id, club_id)
    REFERENCES public.membership_plans (id, club_id) ON DELETE RESTRICT,
  CONSTRAINT membership_renewals_actor_club_fk FOREIGN KEY (actor_user_id, club_id)
    REFERENCES public.users (id, club_id) ON DELETE RESTRICT
);
CREATE INDEX membership_renewals_club_created_idx ON public.membership_renewals (club_id, created_at DESC);
CREATE INDEX membership_renewals_membership_club_created_idx ON public.membership_renewals (membership_id, club_id, created_at DESC);
CREATE INDEX membership_renewals_plan_club_idx ON public.membership_renewals (plan_id, club_id);
CREATE INDEX membership_renewals_actor_club_idx ON public.membership_renewals (actor_user_id, club_id) WHERE actor_user_id IS NOT NULL;

CREATE FUNCTION membercore_internal.validate_renewal_discount_snapshot()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $function$
DECLARE
  v_plan_discount integer;
BEGIN
  SELECT p.ticket_discount_bps INTO v_plan_discount
  FROM public.membership_plans AS p
  WHERE p.id = NEW.plan_id AND p.club_id = NEW.club_id
  FOR SHARE;
  IF v_plan_discount IS NULL OR v_plan_discount <> NEW.discount_bps_snapshot THEN
    RAISE EXCEPTION 'Renewal discount snapshot must match its selected plan at purchase time'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION membercore_internal.validate_renewal_discount_snapshot() FROM PUBLIC;
CREATE TRIGGER membership_renewal_discount_snapshot
  BEFORE INSERT ON public.membership_renewals
  FOR EACH ROW EXECUTE FUNCTION membercore_internal.validate_renewal_discount_snapshot();

-- A dues row represents an amount owed for a renewal. Reconciliation events are
-- append-only and are the evidence of payment/refund; a caller cannot set a
-- fabricated `paid=true` flag. Current payment state is derived from verified events.
CREATE TABLE public.membership_dues (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  membership_id uuid NOT NULL,
  club_id uuid NOT NULL,
  renewal_id uuid NOT NULL,
  amount_paise bigint NOT NULL,
  currency text NOT NULL DEFAULT 'INR',
  idempotency_key uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT membership_dues_amount_check CHECK (amount_paise BETWEEN 0 AND 999999999999),
  CONSTRAINT membership_dues_currency_check CHECK (currency = 'INR'),
  CONSTRAINT membership_dues_idempotency_uq UNIQUE (idempotency_key),
  CONSTRAINT membership_dues_id_scope_uq UNIQUE (id, club_id),
  CONSTRAINT membership_dues_id_renewal_scope_uq UNIQUE (id, renewal_id, club_id),
  CONSTRAINT membership_dues_renewal_uq UNIQUE (renewal_id),
  CONSTRAINT membership_dues_membership_club_fk FOREIGN KEY (membership_id, club_id)
    REFERENCES public.memberships (id, club_id) ON DELETE RESTRICT,
  CONSTRAINT membership_dues_renewal_scope_fk FOREIGN KEY (renewal_id, membership_id, club_id)
    REFERENCES public.membership_renewals (id, membership_id, club_id) ON DELETE RESTRICT
);
CREATE INDEX membership_dues_membership_club_created_idx ON public.membership_dues (membership_id, club_id, created_at DESC);
CREATE INDEX membership_dues_renewal_club_idx ON public.membership_dues (renewal_id, club_id);

CREATE TABLE public.membership_dues_reconciliations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dues_id uuid NOT NULL,
  club_id uuid NOT NULL,
  source text NOT NULL,
  provider text,
  provider_event_id text,
  request_id uuid NOT NULL,
  outcome text NOT NULL,
  verified_at timestamptz NOT NULL,
  verified_by_user_id uuid,
  evidence_reference text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT membership_dues_reconciliation_source_check CHECK (
    source IN ('provider_webhook', 'offline_admin')
  ),
  CONSTRAINT membership_dues_reconciliation_event_check CHECK (
    (source = 'provider_webhook' AND provider IS NOT NULL
      AND length(btrim(provider)) BETWEEN 1 AND 80
      AND provider_event_id IS NOT NULL AND length(btrim(provider_event_id)) BETWEEN 1 AND 255
      AND verified_by_user_id IS NULL)
    OR
    (source = 'offline_admin' AND provider IS NULL AND provider_event_id IS NULL
      AND verified_by_user_id IS NOT NULL)
  ),
  CONSTRAINT membership_dues_reconciliation_outcome_check CHECK (
    outcome IN ('paid', 'failed', 'refunded')
  ),
  CONSTRAINT membership_dues_reconciliation_time_check CHECK (
    isfinite(verified_at) AND isfinite(created_at)
  ),
  CONSTRAINT membership_dues_reconciliation_request_uq UNIQUE (request_id),
  CONSTRAINT membership_dues_reconciliation_id_scope_uq UNIQUE (id, dues_id, club_id),
  CONSTRAINT membership_dues_reconciliation_dues_club_fk FOREIGN KEY (dues_id, club_id)
    REFERENCES public.membership_dues (id, club_id) ON DELETE RESTRICT,
  CONSTRAINT membership_dues_reconciliation_actor_club_fk FOREIGN KEY (verified_by_user_id, club_id)
    REFERENCES public.users (id, club_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX membership_dues_provider_event_uq
  ON public.membership_dues_reconciliations (provider, provider_event_id)
  WHERE provider IS NOT NULL AND provider_event_id IS NOT NULL;
CREATE INDEX membership_dues_reconciliation_dues_created_idx
  ON public.membership_dues_reconciliations (dues_id, club_id, created_at DESC);
CREATE INDEX membership_dues_reconciliation_actor_club_idx
  ON public.membership_dues_reconciliations (verified_by_user_id, club_id)
  WHERE verified_by_user_id IS NOT NULL;

-- Dedicated append-only audit evidence; do not duplicate TrustLedger accounting.
-- JSON snapshots must exclude credentials, tokens, full payment data, and secrets.
CREATE TABLE public.membercore_audit_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  club_id uuid NOT NULL REFERENCES public.clubs(id) ON DELETE RESTRICT,
  actor_user_id uuid,
  target_user_id uuid,
  entity_type text NOT NULL,
  entity_id uuid NOT NULL,
  action text NOT NULL,
  request_id uuid NOT NULL,
  before_state jsonb,
  after_state jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT membercore_audit_entity_check CHECK (entity_type IN ('plan', 'membership', 'renewal', 'dues')),
  CONSTRAINT membercore_audit_action_check CHECK (length(btrim(action)) BETWEEN 1 AND 80),
  CONSTRAINT membercore_audit_request_uq UNIQUE (club_id, request_id),
  CONSTRAINT membercore_audit_actor_club_fk FOREIGN KEY (actor_user_id, club_id)
    REFERENCES public.users (id, club_id) ON DELETE RESTRICT,
  CONSTRAINT membercore_audit_target_club_fk FOREIGN KEY (target_user_id, club_id)
    REFERENCES public.users (id, club_id) ON DELETE RESTRICT
);
CREATE INDEX membercore_audit_club_created_idx ON public.membercore_audit_events (club_id, created_at DESC);
CREATE INDEX membercore_audit_entity_created_idx ON public.membercore_audit_events (entity_type, entity_id, created_at DESC);
CREATE INDEX membercore_audit_actor_club_idx ON public.membercore_audit_events (actor_user_id, club_id) WHERE actor_user_id IS NOT NULL;

-- An entitlement proof is required before a membership can transition to active.
-- A paid grant points at a verified paid reconciliation for the same renewal;
-- a complimentary grant carries a same-club actor and an auditable waiver reason.
CREATE TABLE public.membership_activation_grants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  membership_id uuid NOT NULL,
  club_id uuid NOT NULL,
  renewal_id uuid NOT NULL,
  source text NOT NULL,
  dues_id uuid,
  reconciliation_id uuid,
  waived_by_user_id uuid,
  waiver_reason text,
  request_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT membership_activation_source_check CHECK (
    source IN ('paid_settlement', 'complimentary_waiver')
  ),
  CONSTRAINT membership_activation_source_fields_check CHECK (
    (source = 'paid_settlement' AND dues_id IS NOT NULL AND reconciliation_id IS NOT NULL
      AND waived_by_user_id IS NULL AND waiver_reason IS NULL)
    OR
    (source = 'complimentary_waiver' AND dues_id IS NULL AND reconciliation_id IS NULL
      AND waived_by_user_id IS NOT NULL AND waiver_reason IS NOT NULL
      AND length(btrim(waiver_reason)) BETWEEN 1 AND 500)
  ),
  CONSTRAINT membership_activation_request_uq UNIQUE (request_id),
  CONSTRAINT membership_activation_renewal_uq UNIQUE (renewal_id),
  CONSTRAINT membership_activation_renewal_fk FOREIGN KEY (renewal_id, membership_id, club_id)
    REFERENCES public.membership_renewals (id, membership_id, club_id) ON DELETE RESTRICT,
  CONSTRAINT membership_activation_dues_fk FOREIGN KEY (dues_id, renewal_id, club_id)
    REFERENCES public.membership_dues (id, renewal_id, club_id) ON DELETE RESTRICT,
  CONSTRAINT membership_activation_reconciliation_fk FOREIGN KEY (reconciliation_id, dues_id, club_id)
    REFERENCES public.membership_dues_reconciliations (id, dues_id, club_id) ON DELETE RESTRICT,
  CONSTRAINT membership_activation_waiver_actor_fk FOREIGN KEY (waived_by_user_id, club_id)
    REFERENCES public.users (id, club_id) ON DELETE RESTRICT
);
CREATE INDEX membership_activation_membership_club_idx
  ON public.membership_activation_grants (membership_id, club_id, created_at DESC);
CREATE INDEX membership_activation_waiver_actor_club_idx
  ON public.membership_activation_grants (waived_by_user_id, club_id)
  WHERE waived_by_user_id IS NOT NULL;

-- A renewal can have retries/failures, but only one settlement and one full
-- refund. Partial refunds require a separately approved accounting contract.
CREATE UNIQUE INDEX membership_dues_paid_once_uq
  ON public.membership_dues_reconciliations (dues_id) WHERE outcome = 'paid';
CREATE UNIQUE INDEX membership_dues_refund_once_uq
  ON public.membership_dues_reconciliations (dues_id) WHERE outcome = 'refunded';

CREATE FUNCTION membercore_internal.validate_activation_grant()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $function$
DECLARE
  v_outcome text;
BEGIN
  -- All entitlement writes serialize on membership first, then dues. This also
  -- prevents a refund from slipping between a grant check and its insertion.
  PERFORM 1 FROM public.memberships AS m
  WHERE m.id = NEW.membership_id AND m.club_id = NEW.club_id FOR UPDATE;
  IF NEW.source = 'paid_settlement' THEN
    SELECT r.outcome INTO v_outcome
    FROM public.membership_dues_reconciliations AS r
    WHERE r.id = NEW.reconciliation_id AND r.dues_id = NEW.dues_id
      AND r.club_id = NEW.club_id;
    IF v_outcome IS DISTINCT FROM 'paid' OR EXISTS (
      SELECT 1 FROM public.membership_dues_reconciliations AS r
      WHERE r.dues_id = NEW.dues_id AND r.club_id = NEW.club_id AND r.outcome = 'refunded'
    ) THEN
      RAISE EXCEPTION 'Paid activation requires an unrefunded verified settlement for the same renewal'
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION membercore_internal.validate_activation_grant() FROM PUBLIC;
CREATE TRIGGER membership_activation_grant_validate
  BEFORE INSERT ON public.membership_activation_grants
  FOR EACH ROW EXECUTE FUNCTION membercore_internal.validate_activation_grant();

CREATE FUNCTION membercore_internal.require_activation_before_active()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $function$
DECLARE
  v_renewal public.membership_renewals%ROWTYPE;
BEGIN
  IF NEW.status <> 'active' THEN
    RETURN NEW;
  END IF;
  SELECT r.* INTO v_renewal
  FROM public.membership_renewals AS r
  WHERE r.membership_id = NEW.id AND r.club_id = NEW.club_id
  ORDER BY r.renewal_number DESC
  LIMIT 1;
  IF NOT FOUND
     OR v_renewal.plan_id <> NEW.plan_id
     OR v_renewal.new_starts_at <> NEW.starts_at
     OR v_renewal.new_expires_at <> NEW.expires_at
     OR v_renewal.discount_bps_snapshot <> NEW.discount_bps_snapshot
     OR NOT EXISTS (
       SELECT 1 FROM public.membership_activation_grants AS g
       WHERE g.renewal_id = v_renewal.id
         AND g.membership_id = NEW.id AND g.club_id = NEW.club_id
         AND (g.source = 'complimentary_waiver' OR NOT EXISTS (
           SELECT 1 FROM public.membership_dues_reconciliations AS refunded
           WHERE refunded.dues_id = g.dues_id AND refunded.club_id = g.club_id
             AND refunded.outcome = 'refunded'
         ))
     ) THEN
    RAISE EXCEPTION 'Active membership requires the latest matching renewal and activation proof'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION membercore_internal.require_activation_before_active() FROM PUBLIC;
CREATE TRIGGER membership_activation_required
  BEFORE INSERT OR UPDATE OF status, plan_id, starts_at, expires_at, discount_bps_snapshot
  ON public.memberships
  FOR EACH ROW EXECUTE FUNCTION membercore_internal.require_activation_before_active();

CREATE FUNCTION membercore_internal.lock_reconciliation_membership()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
  -- Acquire the same aggregate lock used by active updates and grant inserts
  -- BEFORE inserting reconciliation evidence. Queries in the later trigger see
  -- any committed competing activation/refund after this lock is acquired.
  PERFORM m.id FROM public.memberships AS m
  JOIN public.membership_dues AS d
    ON d.membership_id = m.id AND d.club_id = m.club_id
  WHERE d.id = NEW.dues_id AND d.club_id = NEW.club_id
  FOR UPDATE OF m;
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION membercore_internal.lock_reconciliation_membership() FROM PUBLIC;
CREATE TRIGGER membership_reconciliation_lock
  BEFORE INSERT ON public.membership_dues_reconciliations
  FOR EACH ROW EXECUTE FUNCTION membercore_internal.lock_reconciliation_membership();

CREATE FUNCTION membercore_internal.enforce_refund_and_revoke_current_term()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $function$
DECLARE
  v_membership_id uuid;
  v_target_user_id uuid;
  v_cancelled_rows integer;
BEGIN
  IF NEW.outcome = 'failed' AND EXISTS (
    SELECT 1 FROM public.membership_dues_reconciliations AS prior
    WHERE prior.dues_id = NEW.dues_id AND prior.outcome = 'paid'
  ) THEN
    RAISE EXCEPTION 'A failed attempt cannot be appended after a settled payment'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.outcome = 'refunded' THEN
    IF NOT EXISTS (SELECT 1 FROM public.membership_dues_reconciliations AS prior
        WHERE prior.dues_id = NEW.dues_id AND prior.outcome = 'paid') THEN
      RAISE EXCEPTION 'A refund requires a prior verified paid settlement'
        USING ERRCODE = '23514';
    END IF;
    SELECT r.membership_id INTO v_membership_id
    FROM public.membership_dues AS d
    JOIN public.membership_renewals AS r
      ON r.id = d.renewal_id AND r.club_id = d.club_id
    WHERE d.id = NEW.dues_id AND d.club_id = NEW.club_id;
    IF v_membership_id IS NOT NULL THEN
      UPDATE public.memberships AS m
      SET status = 'cancelled', updated_at = now()
      FROM public.membership_dues AS d
      JOIN public.membership_renewals AS r
        ON r.id = d.renewal_id AND r.club_id = d.club_id
      WHERE d.id = NEW.dues_id AND d.club_id = NEW.club_id
        AND m.id = v_membership_id AND m.id = r.membership_id
        AND m.club_id = r.club_id AND m.status = 'active'
        AND m.plan_id = r.plan_id AND m.starts_at = r.new_starts_at
        AND m.expires_at = r.new_expires_at
        AND m.discount_bps_snapshot = r.discount_bps_snapshot
      RETURNING m.user_id INTO v_target_user_id;
      GET DIAGNOSTICS v_cancelled_rows = ROW_COUNT;
      IF v_cancelled_rows > 0 THEN
        INSERT INTO public.membercore_audit_events
          (club_id, actor_user_id, target_user_id, entity_type, entity_id, action,
           request_id, after_state)
        VALUES (NEW.club_id, NEW.verified_by_user_id, v_target_user_id, 'membership',
          v_membership_id, 'refunded_current_term_cancelled', NEW.id,
          jsonb_build_object('reconciliation_id', NEW.id, 'outcome', NEW.outcome));
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION membercore_internal.enforce_refund_and_revoke_current_term() FROM PUBLIC;
CREATE TRIGGER membership_refund_revokes_current_term
  AFTER INSERT ON public.membership_dues_reconciliations
  FOR EACH ROW EXECUTE FUNCTION membercore_internal.enforce_refund_and_revoke_current_term();

CREATE FUNCTION membercore_internal.audit_activation_grant()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $function$
DECLARE
  v_target_user_id uuid;
  v_actor_user_id uuid;
BEGIN
  SELECT m.user_id INTO v_target_user_id
  FROM public.memberships AS m
  WHERE m.id = NEW.membership_id AND m.club_id = NEW.club_id;
  IF NEW.source = 'complimentary_waiver' THEN
    v_actor_user_id := NEW.waived_by_user_id;
  ELSE
    SELECT r.verified_by_user_id INTO v_actor_user_id
    FROM public.membership_dues_reconciliations AS r
    WHERE r.id = NEW.reconciliation_id AND r.club_id = NEW.club_id;
  END IF;
  INSERT INTO public.membercore_audit_events
    (club_id, actor_user_id, target_user_id, entity_type, entity_id, action,
     request_id, after_state)
  VALUES
    (NEW.club_id, v_actor_user_id, v_target_user_id, 'membership', NEW.membership_id,
     CASE WHEN NEW.source = 'complimentary_waiver'
       THEN 'complimentary_waiver_granted' ELSE 'paid_membership_activated' END,
     NEW.id,
     jsonb_build_object('source', NEW.source, 'renewal_id', NEW.renewal_id,
       'waiver_reason', NEW.waiver_reason));
  RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION membercore_internal.audit_activation_grant() FROM PUBLIC;
CREATE TRIGGER membership_activation_grant_audit
  AFTER INSERT ON public.membership_activation_grants
  FOR EACH ROW EXECUTE FUNCTION membercore_internal.audit_activation_grant();

-- Keep financial/renewal/audit evidence write-once even for the application DB
-- role. Correct mistakes with a new compensating event; do not rewrite history.
CREATE FUNCTION membercore_internal.reject_history_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
  RAISE EXCEPTION 'MemberCore history is immutable; append a compensating record'
    USING ERRCODE = '55000';
  RETURN NULL;
END
$function$;
REVOKE ALL ON SCHEMA membercore_internal FROM PUBLIC;
REVOKE ALL ON FUNCTION membercore_internal.reject_history_mutation() FROM PUBLIC;

CREATE TRIGGER membership_renewals_immutable
  BEFORE UPDATE OR DELETE ON public.membership_renewals
  FOR EACH ROW EXECUTE FUNCTION membercore_internal.reject_history_mutation();
CREATE TRIGGER membership_dues_immutable
  BEFORE UPDATE OR DELETE ON public.membership_dues
  FOR EACH ROW EXECUTE FUNCTION membercore_internal.reject_history_mutation();
CREATE TRIGGER membership_dues_reconciliations_immutable
  BEFORE UPDATE OR DELETE ON public.membership_dues_reconciliations
  FOR EACH ROW EXECUTE FUNCTION membercore_internal.reject_history_mutation();
CREATE TRIGGER membership_activation_grants_immutable
  BEFORE UPDATE OR DELETE ON public.membership_activation_grants
  FOR EACH ROW EXECUTE FUNCTION membercore_internal.reject_history_mutation();
CREATE TRIGGER membercore_audit_events_immutable
  BEFORE UPDATE OR DELETE ON public.membercore_audit_events
  FOR EACH ROW EXECUTE FUNCTION membercore_internal.reject_history_mutation();

