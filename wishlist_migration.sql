-- Minto user wishlist
-- Run once in Supabase SQL Editor. Safe to re-run.
BEGIN;
CREATE TABLE IF NOT EXISTS public.wishlists (
    id bigserial PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 120),
    target_amount numeric(14,2) NOT NULL CHECK (target_amount > 0),
    saved_amount numeric(14,2) NOT NULL DEFAULT 0 CHECK (saved_amount >= 0),
    target_date date,
    priority text NOT NULL DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high')),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'purchased')),
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (saved_amount <= target_amount)
);
ALTER TABLE public.wishlists ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "users manage own wishlist" ON public.wishlists;
CREATE POLICY "users manage own wishlist" ON public.wishlists FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE INDEX IF NOT EXISTS wishlists_user_status_created_idx ON public.wishlists (user_id, status, created_at DESC);
COMMIT;
