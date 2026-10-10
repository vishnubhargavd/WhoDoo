-- Migration 071: Member Profiles and Renewal Reminders
-- Adds editable profile fields to public.users and introduces member_renewal_reminders.

ALTER TABLE public.users ADD COLUMN IF NOT EXISTS phone text NULL;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS department text NULL;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS academic_year text NULL;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS profile_image_url text NULL;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS public.member_renewal_reminders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  club_id uuid NOT NULL REFERENCES public.clubs(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  membership_id uuid NOT NULL,
  plan_name text NOT NULL,
  reminder_stage text NOT NULL,
  expires_at timestamptz NOT NULL,
  message text NOT NULL,
  action_url text NOT NULL DEFAULT '/member/renew',
  is_dismissed boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  dismissed_at timestamptz NULL,
  CONSTRAINT member_renewal_stage_check CHECK (reminder_stage IN ('30_days', '14_days', '7_days', '1_day', 'on_expiry')),
  CONSTRAINT uq_member_reminder_stage UNIQUE (membership_id, reminder_stage)
);

CREATE INDEX IF NOT EXISTS member_renewal_reminders_user_idx ON public.member_renewal_reminders (user_id, is_dismissed);
CREATE INDEX IF NOT EXISTS member_renewal_reminders_club_idx ON public.member_renewal_reminders (club_id, expires_at);
