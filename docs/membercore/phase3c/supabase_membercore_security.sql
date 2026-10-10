-- PROPOSAL ONLY — Supabase-specific, unapplied add-on.
-- Requires the approved shared users/clubs schema and portable 030 + 031.
-- Keep separate from portable migrations. The selected runner must wrap this
-- file in its own transaction; never execute it against live Supabase now.

DO $preflight$
BEGIN
  IF to_regclass('auth.users') IS NULL OR to_regclass('public.users') IS NULL THEN
    RAISE EXCEPTION 'Supabase add-on requires auth.users and public.users';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name='users' AND column_name='auth_user_id'
        AND data_type='uuid' AND is_nullable='YES') THEN
    RAISE EXCEPTION 'Supabase add-on requires nullable public.users.auth_user_id';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
      WHERE table_schema='auth' AND table_name='users' AND column_name='id' AND data_type='uuid') THEN
    RAISE EXCEPTION 'auth.users.id must be UUID';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_constraint
      WHERE conrelid='public.users'::regclass AND conname='users_auth_user_id_auth_users_fk') THEN
    RAISE EXCEPTION 'Constraint users_auth_user_id_auth_users_fk already exists; inspect it before applying';
  END IF;
END
$preflight$;

ALTER TABLE public.users
  ADD CONSTRAINT users_auth_user_id_auth_users_fk
  FOREIGN KEY (auth_user_id) REFERENCES auth.users(id) ON DELETE RESTRICT;

-- This non-exposed helper returns only the caller's mapped app identity. Its
-- owner must be a trusted migration role that can read public.users. Keep the
-- fixed empty search_path and fully qualified objects; never add caller input.
CREATE SCHEMA membercore_private;
REVOKE ALL ON SCHEMA membercore_private FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA membercore_private TO authenticated;

CREATE FUNCTION membercore_private.current_application_identity()
RETURNS TABLE (application_user_id uuid, club_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $function$
  SELECT u.id, u.club_id
  FROM public.users AS u
  WHERE u.auth_user_id = (SELECT auth.uid())
  LIMIT 1
$function$;
REVOKE ALL ON FUNCTION membercore_private.current_application_identity() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION membercore_private.current_application_identity() TO authenticated;

-- These are newly proposed tables only. Do not alter RLS/grants on shared users,
-- clubs, events, finance or volunteering tables in this proposal.
ALTER TABLE public.membership_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.membership_renewals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.membership_dues ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.membership_dues_reconciliations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.membership_activation_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.membercore_audit_events ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.membership_plans, public.memberships, public.membership_renewals,
  public.membership_dues, public.membership_dues_reconciliations,
  public.membership_activation_grants, public.membercore_audit_events
  FROM PUBLIC, anon, authenticated;

-- Read-only, least-privilege client contract. There are intentionally no client
-- INSERT/UPDATE/DELETE grants or policies; trusted backend transactions write.
GRANT SELECT ON public.membership_plans, public.memberships, public.membership_renewals
  TO authenticated;
GRANT SELECT (id, membership_id, club_id, renewal_id, amount_paise, currency, created_at)
  ON public.membership_dues TO authenticated;
GRANT SELECT (id, dues_id, club_id, outcome, verified_at, created_at)
  ON public.membership_dues_reconciliations TO authenticated;
-- membercore_audit_events has no client grant or policy by design.

CREATE POLICY membership_plans_mapped_same_club_read
  ON public.membership_plans FOR SELECT TO authenticated
  USING (
    club_id = (SELECT i.club_id
      FROM membercore_private.current_application_identity() AS i)
    AND (
      is_active OR EXISTS (
        SELECT 1 FROM public.memberships AS m
        WHERE m.plan_id = membership_plans.id
          AND m.club_id = membership_plans.club_id
          AND m.user_id = (SELECT i.application_user_id
            FROM membercore_private.current_application_identity() AS i)
      )
    )
  );
CREATE POLICY memberships_own_application_user_read
  ON public.memberships FOR SELECT TO authenticated
  USING (
    user_id = (SELECT i.application_user_id
      FROM membercore_private.current_application_identity() AS i)
  );
CREATE POLICY membership_renewals_own_membership_read
  ON public.membership_renewals FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM public.memberships AS m
      WHERE m.id = membership_renewals.membership_id
        AND m.club_id = membership_renewals.club_id
        AND m.user_id = (SELECT i.application_user_id
          FROM membercore_private.current_application_identity() AS i)
    )
  );
CREATE POLICY membership_dues_own_membership_read
  ON public.membership_dues FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM public.memberships AS m
      WHERE m.id = membership_dues.membership_id
        AND m.club_id = membership_dues.club_id
        AND m.user_id = (SELECT i.application_user_id
          FROM membercore_private.current_application_identity() AS i)
    )
  );
CREATE POLICY membership_dues_reconciliations_own_membership_read
  ON public.membership_dues_reconciliations FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1
      FROM public.membership_dues AS d
      JOIN public.memberships AS m
        ON m.id = d.membership_id AND m.club_id = d.club_id
      WHERE d.id = membership_dues_reconciliations.dues_id
        AND d.club_id = membership_dues_reconciliations.club_id
        AND m.user_id = (SELECT i.application_user_id
          FROM membercore_private.current_application_identity() AS i)
    )
  );
-- No policy for audit events; RLS plus absent grants denies client access.

