-- Per-friend sharing controls for Minto Friends.
-- Apply in Supabase SQL Editor after schema.sql, minto_v2_migration.sql,
-- and wishlist_migration.sql. Safe to re-run.
begin;

alter table public.profiles add column if not exists phone text;
alter table public.profiles add column if not exists share_phone_with_friends boolean not null default false;
alter table public.profiles add column if not exists share_email_with_friends boolean not null default false;

create table if not exists public.friend_sharing_preferences (
    owner_user_id uuid not null references auth.users(id) on delete cascade,
    friend_user_id uuid not null references auth.users(id) on delete cascade,
    share_email boolean not null default false,
    share_phone boolean not null default false,
    share_wishlist boolean not null default false,
    updated_at timestamptz not null default now(),
    primary key (owner_user_id, friend_user_id),
    constraint friend_sharing_preferences_not_self check (owner_user_id <> friend_user_id)
);
alter table public.friend_sharing_preferences enable row level security;
drop policy if exists "users manage own friend sharing preferences" on public.friend_sharing_preferences;
create policy "users manage own friend sharing preferences"
    on public.friend_sharing_preferences for all
    using (auth.uid() = owner_user_id)
    with check (auth.uid() = owner_user_id);
create index if not exists friend_sharing_preferences_friend_idx
    on public.friend_sharing_preferences (friend_user_id, owner_user_id);

create or replace function public.set_minto_friend_sharing_preferences(
    p_username text,
    p_share_email boolean default false,
    p_share_phone boolean default false,
    p_share_wishlist boolean default false
)
returns boolean
language plpgsql security definer set search_path = public, auth, pg_temp as $$
declare
    v_friend_id uuid;
begin
    if auth.uid() is null then raise exception 'Sign in to manage sharing preferences.'; end if;
    select p.id into v_friend_id
      from public.profiles p
     where lower(p.username) = lower(btrim(coalesce(p_username, '')))
       and p.id <> auth.uid()
     limit 1;
    if v_friend_id is null then raise exception 'Friend not found.'; end if;
    if not exists (
        select 1 from public.friends f
         where f.user_a = least(auth.uid(), v_friend_id)
           and f.user_b = greatest(auth.uid(), v_friend_id)
    ) then raise exception 'Sharing preferences can only be set for an accepted friend.'; end if;

    insert into public.friend_sharing_preferences
        (owner_user_id, friend_user_id, share_email, share_phone, share_wishlist, updated_at)
    values (auth.uid(), v_friend_id, coalesce(p_share_email,false),
            coalesce(p_share_phone,false), coalesce(p_share_wishlist,false), now())
    on conflict (owner_user_id, friend_user_id) do update
       set share_email = excluded.share_email,
           share_phone = excluded.share_phone,
           share_wishlist = excluded.share_wishlist,
           updated_at = now();
    return true;
end;
$$;

create or replace function public.list_minto_friend_sharing_preferences()
returns table(username text, share_email boolean, share_phone boolean,
              share_wishlist boolean, shared_wishlist_available boolean)
language sql stable security definer set search_path = public, pg_temp as $$
    select p.username,
           coalesce(mine.share_email, false),
           coalesce(mine.share_phone, false),
           coalesce(mine.share_wishlist, false),
           coalesce(theirs.share_wishlist, false)
      from public.friends f
      join public.profiles p
        on p.id = case when f.user_a = auth.uid() then f.user_b else f.user_a end
      left join public.friend_sharing_preferences mine
        on mine.owner_user_id = auth.uid() and mine.friend_user_id = p.id
      left join public.friend_sharing_preferences theirs
        on theirs.owner_user_id = p.id and theirs.friend_user_id = auth.uid()
     where auth.uid() in (f.user_a, f.user_b)
     order by lower(coalesce(p.username, ''));
$$;

-- Contacts are returned only when the other friend has explicitly opted in
-- for the current user. The existing global contact switches remain an
-- additional privacy gate.
create or replace function public.list_minto_friend_contacts()
returns table(username text, phone text, email text)
language sql stable security definer set search_path = public, auth, pg_temp as $$
    select p.username,
           case when coalesce(pref.share_phone, false)
                     and coalesce(p.share_phone_with_friends, false)
                then nullif(btrim(p.phone), '') else null end as phone,
           case when coalesce(pref.share_email, false)
                     and coalesce(p.share_email_with_friends, false)
                then nullif(btrim(u.email), '') else null end as email
      from public.friends f
      join public.profiles p
        on p.id = case when f.user_a = auth.uid() then f.user_b else f.user_a end
      join auth.users u on u.id = p.id
      left join public.friend_sharing_preferences pref
        on pref.owner_user_id = p.id and pref.friend_user_id = auth.uid()
     where auth.uid() in (f.user_a, f.user_b)
       and (coalesce(pref.share_phone,false) or coalesce(pref.share_email,false));
$$;

create or replace function public.get_minto_friend_shared_wishlist(p_username text)
returns table(display_name text, name text, target_amount numeric, saved_amount numeric,
              target_date date, priority text, status text, notes text)
language plpgsql stable security definer set search_path = public, auth, pg_temp as $$
declare
    v_friend_id uuid;
begin
    if auth.uid() is null then raise exception 'Sign in to view a shared wishlist.'; end if;
    select p.id into v_friend_id
      from public.profiles p
     where lower(p.username) = lower(btrim(coalesce(p_username, '')))
       and p.id <> auth.uid()
     limit 1;
    if v_friend_id is null then return; end if;
    if not exists (
        select 1 from public.friends f
         where f.user_a = least(auth.uid(), v_friend_id)
           and f.user_b = greatest(auth.uid(), v_friend_id)
    ) then return; end if;
    if not exists (
        select 1 from public.friend_sharing_preferences pref
         where pref.owner_user_id = v_friend_id
           and pref.friend_user_id = auth.uid()
           and pref.share_wishlist = true
    ) then return; end if;

    return query
    select coalesce(nullif(btrim(p.display_name), ''), p.username),
           w.name, w.target_amount, w.saved_amount, w.target_date, w.priority,
           w.status, w.notes
      from public.wishlists w
      join public.profiles p on p.id = v_friend_id
     where w.user_id = v_friend_id
     order by case w.priority when 'high' then 1 when 'medium' then 2 else 3 end,
              w.created_at desc;
end;
$$;

revoke all on function public.set_minto_friend_sharing_preferences(text, boolean, boolean, boolean) from public;
revoke all on function public.list_minto_friend_sharing_preferences() from public;
revoke all on function public.list_minto_friend_contacts() from public;
revoke all on function public.get_minto_friend_shared_wishlist(text) from public;
grant execute on function public.set_minto_friend_sharing_preferences(text, boolean, boolean, boolean) to authenticated;
grant execute on function public.list_minto_friend_sharing_preferences() to authenticated;
grant execute on function public.list_minto_friend_contacts() to authenticated;
grant execute on function public.get_minto_friend_shared_wishlist(text) to authenticated;

commit;
