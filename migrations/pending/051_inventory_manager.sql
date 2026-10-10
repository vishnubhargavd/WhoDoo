-- MerchFlow: Dedicated Club Inventory Manager Assignment
CREATE TABLE IF NOT EXISTS public.club_inventory_managers (
    club_id     uuid PRIMARY KEY,
    user_id     uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    assigned_by uuid NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
    assigned_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS club_inventory_managers_user_idx
    ON public.club_inventory_managers (user_id);
