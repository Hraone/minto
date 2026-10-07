-- Adds an authenticated, boolean-only username availability check for the profile editor.
-- Safe to rerun; database uniqueness remains enforced by profiles_username_lower_uidx.
begin;

create or replace function public.minto_username_is_available(p_username text)
returns boolean
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
    v_username text := lower(btrim(coalesce(p_username, '')));
begin
    if auth.uid() is null or v_username !~ '^[a-z0-9_]{3,24}$' then
        return false;
    end if;

    return not exists (
        select 1
        from public.profiles p
        where lower(p.username) = v_username
          and p.id <> auth.uid()
    );
end;
$$;

revoke all on function public.minto_username_is_available(text) from public, anon;
grant execute on function public.minto_username_is_available(text) to authenticated;

commit;

