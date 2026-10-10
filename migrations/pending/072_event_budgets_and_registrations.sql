-- Migration 072: Event Budgets and Registrations
-- Adds budget in INR and overall participant registration capacity and registration window to events.

ALTER TABLE public.events ADD COLUMN IF NOT EXISTS budget_inr numeric(12,2) NOT NULL DEFAULT 0.00;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS registration_capacity integer NULL;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS registration_opens_at timestamptz NULL;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS registration_closes_at timestamptz NULL;
