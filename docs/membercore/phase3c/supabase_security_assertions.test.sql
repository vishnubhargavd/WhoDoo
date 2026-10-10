-- Supabase-local only catalog assertions. Run after the Supabase-only proposal has
-- been applied to a disposable local Supabase project. Does not alter any data.
\set ON_ERROR_STOP on
BEGIN;
DO $assertions$
DECLARE
  v_table text;
  v_tables text[] := ARRAY[
    'membership_plans', 'memberships', 'membership_renewals', 'membership_dues',
    'membership_dues_reconciliations', 'membership_activation_grants',
    'membercore_audit_events'
  ];
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
      WHERE conrelid='public.users'::regclass
        AND conname='users_auth_user_id_auth_users_fk' AND contype='f') THEN
    RAISE EXCEPTION 'Auth mapping FK to auth.users is missing';
  END IF;
  FOREACH v_table IN ARRAY v_tables LOOP
    IF NOT (SELECT relrowsecurity FROM pg_class
        WHERE oid=format('public.%I', v_table)::regclass) THEN
      RAISE EXCEPTION 'RLS is disabled for public.%', v_table;
    END IF;
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM pg_policies
      WHERE schemaname='public' AND tablename='memberships'
        AND policyname='memberships_own_application_user_read' AND cmd='SELECT'
        AND roles @> ARRAY['authenticated']::name[]) THEN
    RAISE EXCEPTION 'Own-membership read policy is missing';
  END IF;
  IF (SELECT count(*) FROM pg_policies
      WHERE schemaname='public' AND tablename='membercore_audit_events') <> 0 THEN
    RAISE EXCEPTION 'Audit events must have no client policies';
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.role_table_grants
      WHERE table_schema='public'
        AND table_name::text = ANY(v_tables)
        AND grantee IN ('anon','authenticated')
        AND privilege_type IN ('INSERT','UPDATE','DELETE')) THEN
    RAISE EXCEPTION 'Client write grant exists on a MemberCore table';
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.role_table_grants
      WHERE table_schema='public' AND table_name::text = ANY(v_tables)
        AND grantee='anon') THEN
    RAISE EXCEPTION 'Anonymous role has a MemberCore table grant';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_policies
      WHERE schemaname='public' AND tablename::text = ANY(v_tables) AND cmd <> 'SELECT') THEN
    RAISE EXCEPTION 'A MemberCore table has a non-SELECT client policy';
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.role_table_grants
      WHERE table_schema='public' AND table_name::text='membercore_audit_events'
        AND grantee IN ('anon','authenticated')) THEN
    RAISE EXCEPTION 'Audit event table must have no client grant';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_proc p
      JOIN pg_namespace n ON n.oid=p.pronamespace
      JOIN pg_roles r ON r.oid=p.proowner
      WHERE n.nspname='membercore_private'
        AND p.proname='current_application_identity'
        AND p.prosecdef
        AND r.rolbypassrls
        AND p.proconfig @> ARRAY['search_path=""']) THEN
    RAISE EXCEPTION 'Identity helper must be fixed-search-path SECURITY DEFINER owned by a reviewed BYPASSRLS role';
  END IF;
  IF has_function_privilege('anon',
      'membercore_private.current_application_identity()', 'EXECUTE') THEN
    RAISE EXCEPTION 'Anonymous role can execute the identity helper';
  END IF;
  IF NOT has_function_privilege('authenticated',
      'membercore_private.current_application_identity()', 'EXECUTE') THEN
    RAISE EXCEPTION 'Authenticated role cannot execute the identity helper';
  END IF;
END
$assertions$;
ROLLBACK;
