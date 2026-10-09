-- Minto: opt-in phone/email sharing with accepted friends only.
-- Safe to rerun. Run in the Supabase SQL Editor.

ALTER TABLE public.profiles
    ADD COLUMN IF NOT EXISTS phone TEXT,
    ADD COLUMN IF NOT EXISTS share_phone_with_friends BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS share_email_with_friends BOOLEAN NOT NULL DEFAULT FALSE;

CREATE OR REPLACE FUNCTION public.list_minto_friend_contacts()
RETURNS TABLE (
    username TEXT,
    phone TEXT,
    email TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, auth, pg_temp
AS $$
    SELECT
        p.username,
        CASE WHEN p.share_phone_with_friends THEN NULLIF(BTRIM(p.phone), '') ELSE NULL END,
        CASE WHEN p.share_email_with_friends THEN NULLIF(BTRIM(u.email), '') ELSE NULL END
    FROM public.friends f
    JOIN public.profiles p
      ON p.id = CASE
          WHEN f.user_a = auth.uid() THEN f.user_b
          ELSE f.user_a
      END
    LEFT JOIN auth.users u ON u.id = p.id
    WHERE auth.uid() IS NOT NULL
      AND auth.uid() IN (f.user_a, f.user_b)
      AND p.username IS NOT NULL;
$$;

REVOKE ALL ON FUNCTION public.list_minto_friend_contacts() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.list_minto_friend_contacts() TO authenticated;
