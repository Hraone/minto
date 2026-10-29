-- Minto profile colour palette preference.
-- Run this migration in the Supabase SQL Editor before deploying the feature.
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS color_theme text NOT NULL DEFAULT 'grove';

UPDATE public.profiles
SET color_theme = 'grove'
WHERE color_theme IS NULL OR color_theme NOT IN ('grove', 'tide', 'rosewater', 'terra', 'ink');

ALTER TABLE public.profiles
  DROP CONSTRAINT IF EXISTS profiles_color_theme_check;

ALTER TABLE public.profiles
  ADD CONSTRAINT profiles_color_theme_check
  CHECK (color_theme IN ('grove', 'tide', 'rosewater', 'terra', 'ink'));
