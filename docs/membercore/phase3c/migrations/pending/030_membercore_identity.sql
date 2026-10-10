-- PROPOSAL ONLY — portable SQL migration 030 for app/db/migrate.py.
-- Keep in migrations/pending/ until approved. The runner owns the transaction.
-- This migration deliberately contains no Supabase auth schema or RLS dependency.

DO $preflight$
DECLARE
  v_type text;
BEGIN
  IF to_regclass('public.clubs') IS NULL OR to_regclass('public.users') IS NULL THEN
    RAISE EXCEPTION '030 requires shared public.clubs and public.users';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name='clubs' AND column_name='id' AND data_type='uuid')
     OR NOT EXISTS (SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name='users' AND column_name='id' AND data_type='uuid')
     OR NOT EXISTS (SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name='users' AND column_name='club_id' AND data_type='uuid')
     OR NOT EXISTS (SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name='users' AND column_name='is_active_member' AND data_type='boolean') THEN
    RAISE EXCEPTION '030 shared clubs/users types do not match the approved UUID/boolean contract';
  END IF;

  SELECT data_type INTO v_type FROM information_schema.columns
    WHERE table_schema='public' AND table_name='users' AND column_name='auth_user_id';
  IF v_type IS NOT NULL AND v_type <> 'uuid' THEN
    RAISE EXCEPTION 'Existing public.users.auth_user_id is not UUID; review before continuing';
  END IF;
  IF v_type = 'uuid' AND EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='public' AND table_name='users' AND column_name='auth_user_id'
      AND is_nullable <> 'YES'
  ) THEN
    RAISE EXCEPTION 'Existing public.users.auth_user_id is NOT NULL; review before continuing';
  END IF;

  IF to_regclass('public.users_auth_user_id_uq') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1 FROM pg_index i
       JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attname = 'auth_user_id'
       WHERE i.indexrelid = to_regclass('public.users_auth_user_id_uq')
         AND i.indrelid = 'public.users'::regclass AND i.indisunique
         AND i.indisvalid AND i.indnkeyatts = 1 AND i.indkey[0] = a.attnum
         AND i.indpred IS NULL AND i.indexprs IS NULL
     ) THEN
    RAISE EXCEPTION 'Index name public.users_auth_user_id_uq already exists with an unexpected definition';
  END IF;
  IF to_regclass('public.users_id_club_id_uq') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1 FROM pg_indexes
       WHERE schemaname='public' AND indexname='users_id_club_id_uq'
         AND indexdef ILIKE 'CREATE UNIQUE INDEX%ON public.users USING btree (id, club_id)%'
     ) THEN
    RAISE EXCEPTION 'Index name public.users_id_club_id_uq already exists with an unexpected definition';
  END IF;
END
$preflight$;

ALTER TABLE public.users ADD COLUMN IF NOT EXISTS auth_user_id uuid NULL;
CREATE UNIQUE INDEX IF NOT EXISTS users_auth_user_id_uq
  ON public.users (auth_user_id);
-- Ordinary UNIQUE allows multiple NULL mappings and matches the existing
-- FastAPI identity bridge's non-partial unique-index catalog contract.
-- Supports club-consistent composite FKs without changing the existing PK or IDs.
CREATE UNIQUE INDEX IF NOT EXISTS users_id_club_id_uq
  ON public.users (id, club_id);

