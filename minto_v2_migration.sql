-- Minto 2.0.0 foundation: usernames, friendships, linked trip members,
-- share acknowledgements, and explicit account movements.
-- Run once in the Supabase SQL Editor after the existing schema.sql and
-- trip_mode_v1_5_migration.sql. The migration is additive and safe to rerun.

begin;

alter table public.profiles add column if not exists username text;
alter table public.profiles add column if not exists default_expense_source_id integer
    references public.user_sources(id) on delete set null;
create unique index if not exists profiles_username_lower_uidx
    on public.profiles (lower(username)) where username is not null;
alter table public.profiles drop constraint if exists profiles_username_check;
alter table public.profiles add constraint profiles_username_check
    check (username is null or username ~ '^[a-z0-9_]{3,24}$');

create table if not exists public.friend_requests (
    id bigserial primary key,
    sender_user_id uuid not null references auth.users(id) on delete cascade,
    receiver_user_id uuid not null references auth.users(id) on delete cascade,
    status text not null default 'pending',
    created_at timestamptz not null default now(),
    responded_at timestamptz,
    constraint friend_requests_not_self check (sender_user_id <> receiver_user_id),
    constraint friend_requests_status_check check (status in ('pending', 'accepted', 'rejected'))
);
create unique index if not exists friend_requests_one_active_pair_idx
    on public.friend_requests (
        least(sender_user_id, receiver_user_id),
        greatest(sender_user_id, receiver_user_id)
    ) where status = 'pending';
create index if not exists friend_requests_sender_idx
    on public.friend_requests (sender_user_id, status, created_at desc);
create index if not exists friend_requests_receiver_idx
    on public.friend_requests (receiver_user_id, status, created_at desc);

create table if not exists public.friends (
    user_a uuid not null references auth.users(id) on delete cascade,
    user_b uuid not null references auth.users(id) on delete cascade,
    created_at timestamptz not null default now(),
    primary key (user_a, user_b),
    constraint friends_ordered_pair_check check (user_a < user_b)
);
create index if not exists friends_user_b_idx on public.friends (user_b, user_a);

create table if not exists public.trip_members (
    id bigserial primary key,
    trip_id integer not null references public.trips(id) on delete cascade,
    user_id uuid references auth.users(id) on delete set null,
    guest_name text,
    display_name text not null,
    profile_emoji text,
    role text not null default 'member',
    active boolean not null default true,
    removed_at timestamptz,
    created_at timestamptz not null default now(),
    constraint trip_members_role_check check (role in ('owner', 'member')),
    constraint trip_members_identity_check check (
        (user_id is not null and guest_name is null)
        or (user_id is null and guest_name is not null and btrim(guest_name) <> '')
    )
);
create unique index if not exists trip_members_user_uidx
    on public.trip_members (trip_id, user_id) where user_id is not null;
create unique index if not exists trip_members_guest_name_uidx
    on public.trip_members (trip_id, lower(guest_name)) where guest_name is not null;
create index if not exists trip_members_trip_active_idx
    on public.trip_members (trip_id, active, id);
create index if not exists trip_members_user_idx
    on public.trip_members (user_id, active, trip_id) where user_id is not null;

-- Keep a removed/deleted account's snapshot as a guest identity so old shares
-- and settlements remain readable after the auth.users row is removed.
create or replace function public.preserve_trip_member_snapshot_on_user_delete()
returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
begin
    update public.trip_members tm
       set guest_name = case when exists (
               select 1 from public.trip_members other
                where other.trip_id=tm.trip_id and other.id<>tm.id
                  and lower(coalesce(other.guest_name, other.display_name))=lower(coalesce(nullif(btrim(tm.display_name), ''), 'Former Minto member'))
           ) then coalesce(nullif(btrim(tm.display_name), ''), 'Former Minto member') || ' (Minto member)'
           else coalesce(nullif(btrim(tm.display_name), ''), 'Former Minto member') end,
           user_id = null,
           active = false,
           removed_at = coalesce(removed_at, now())
     where tm.user_id = old.id;
    return old;
end;
$$;
drop trigger if exists preserve_trip_member_snapshot_on_user_delete on auth.users;
create trigger preserve_trip_member_snapshot_on_user_delete
before delete on auth.users
for each row execute function public.preserve_trip_member_snapshot_on_user_delete();

-- Every trip gets a durable owner membership row. Existing guests are copied
-- from trip_participants; no legacy trip data is removed.
insert into public.trip_members (trip_id, user_id, guest_name, display_name, profile_emoji, role)
select t.id, t.user_id, null,
       coalesce(nullif(btrim(p.display_name), ''), 'Minto member'), p.profile_emoji, 'owner'
  from public.trips t
  left join public.profiles p on p.id = t.user_id
on conflict do nothing;

insert into public.trip_members (trip_id, user_id, guest_name, display_name, role)
select tp.trip_id, null, tp.name, tp.name, 'member'
  from public.trip_participants tp
on conflict do nothing;

-- Preserve names that are present in an old split/payer row even if a legacy
-- participant row was removed before this migration.
insert into public.trip_members (trip_id, user_id, guest_name, display_name, role)
select src.trip_id, null, src.member_name, src.member_name, 'member'
  from (
      select e.trip_id, s.participant_name as member_name
        from public.trip_expense_splits s
        join public.trip_expenses e on e.id = s.trip_expense_id
      union
      select e.trip_id, e.paid_by
        from public.trip_expenses e
       where lower(e.paid_by) <> 'you'
  ) src
 where nullif(btrim(src.member_name), '') is not null
   and lower(src.member_name) <> 'you'
   and not exists (
       select 1 from public.trip_members tm
        where tm.trip_id = src.trip_id
          and lower(coalesce(tm.guest_name, tm.display_name)) = lower(src.member_name)
   )
on conflict do nothing;

alter table public.trip_expenses add column if not exists payer_member_id bigint
    references public.trip_members(id) on delete set null;
alter table public.trip_expenses add column if not exists payment_transaction_id integer;

update public.trip_expenses e
   set payer_member_id = tm.id
  from public.trip_members tm
 where e.payer_member_id is null
   and tm.trip_id = e.trip_id
   and ((lower(e.paid_by) = 'you' and tm.role = 'owner')
        or lower(coalesce(tm.guest_name, tm.display_name)) = lower(e.paid_by));

alter table public.trip_expense_splits add column if not exists trip_member_id bigint
    references public.trip_members(id) on delete set null;
update public.trip_expense_splits s
   set trip_member_id = tm.id
  from public.trip_expenses e, public.trip_members tm
 where e.id = s.trip_expense_id
   and tm.trip_id = e.trip_id
   and s.trip_member_id is null
   and ((lower(s.participant_name) = 'you' and tm.role = 'owner')
        or lower(coalesce(tm.guest_name, tm.display_name)) = lower(s.participant_name));
create index if not exists trip_expense_splits_member_idx
    on public.trip_expense_splits (trip_member_id, trip_expense_id);

create table if not exists public.trip_expense_shares (
    id bigserial primary key,
    trip_expense_id integer not null references public.trip_expenses(id) on delete cascade,
    trip_member_id bigint not null references public.trip_members(id) on delete restrict,
    amount numeric not null check (amount > 0),
    status text not null default 'pending',
    personal_transaction_id integer unique,
    payer_transaction_id integer unique,
    created_at timestamptz not null default now(),
    responded_at timestamptz,
    constraint trip_expense_shares_status_check
        check (status in ('pending', 'accepted', 'rejected', 'settled')),
    constraint trip_expense_shares_expense_member_key unique (trip_expense_id, trip_member_id)
);
create index if not exists trip_expense_shares_member_status_idx
    on public.trip_expense_shares (trip_member_id, status, trip_expense_id);
create index if not exists trip_expense_shares_expense_idx
    on public.trip_expense_shares (trip_expense_id, status);

insert into public.trip_expense_shares (trip_expense_id, trip_member_id, amount, status)
select s.trip_expense_id, s.trip_member_id, s.share_amount, 'accepted'
  from public.trip_expense_splits s
 where s.trip_member_id is not null and s.share_amount > 0
on conflict (trip_expense_id, trip_member_id) do nothing;

alter table public.transactions add column if not exists affects_source_balance boolean not null default true;
alter table public.transactions add column if not exists trip_expense_id integer
    references public.trip_expenses(id) on delete set null;
alter table public.transactions add column if not exists trip_expense_share_id bigint;
alter table public.transactions add column if not exists trip_settlement_id bigint;
alter table public.transactions add column if not exists trip_share_status text;
alter table public.transactions drop constraint if exists transactions_category_check;
alter table public.transactions add constraint transactions_category_check
    check (category in (
        'investment', 'expense', 'income', 'transfer', 'lending', 'unknown',
        'trip_expense_payment', 'trip_settlement'
    ));
alter table public.transactions drop constraint if exists transactions_trip_share_status_check;
alter table public.transactions add constraint transactions_trip_share_status_check
    check (trip_share_status is null or trip_share_status in ('pending', 'accepted', 'rejected', 'settled'));

do $$ begin
    if not exists (select 1 from pg_constraint where conname = 'trip_expenses_payment_transaction_fk') then
        alter table public.trip_expenses add constraint trip_expenses_payment_transaction_fk
            foreign key (payment_transaction_id) references public.transactions(id) on delete set null;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'trip_transactions_share_fk') then
        alter table public.transactions add constraint trip_transactions_share_fk
            foreign key (trip_expense_share_id) references public.trip_expense_shares(id) on delete set null;
    end if;
end $$;

do $$ begin
    if not exists (select 1 from pg_constraint where conname = 'trip_share_personal_transaction_fk') then
        alter table public.trip_expense_shares add constraint trip_share_personal_transaction_fk
            foreign key (personal_transaction_id) references public.transactions(id) on delete set null;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'trip_share_payer_transaction_fk') then
        alter table public.trip_expense_shares add constraint trip_share_payer_transaction_fk
            foreign key (payer_transaction_id) references public.transactions(id) on delete set null;
    end if;
end $$;
create index if not exists transactions_trip_expense_idx on public.transactions (trip_expense_id);
create index if not exists transactions_trip_share_idx on public.transactions (trip_expense_share_id);

alter table public.trip_settlements add column if not exists from_member_id bigint
    references public.trip_members(id) on delete set null;
alter table public.trip_settlements add column if not exists to_member_id bigint
    references public.trip_members(id) on delete set null;
alter table public.trip_settlements add column if not exists from_source_id integer
    references public.user_sources(id) on delete set null;
alter table public.trip_settlements add column if not exists to_source_id integer
    references public.user_sources(id) on delete set null;
alter table public.trip_settlements add column if not exists from_transaction_id integer;
alter table public.trip_settlements add column if not exists to_transaction_id integer;
alter table public.trip_settlements add column if not exists from_reversal_transaction_id integer;
alter table public.trip_settlements add column if not exists to_reversal_transaction_id integer;
alter table public.trip_settlements add column if not exists received_at timestamptz;

update public.trip_settlements s
   set from_member_id = tm.id
  from public.trip_members tm
 where s.from_member_id is null and tm.trip_id = s.trip_id
   and ((lower(s.from_person) = 'you' and tm.role = 'owner')
        or lower(coalesce(tm.guest_name, tm.display_name)) = lower(s.from_person));
update public.trip_settlements s
   set to_member_id = tm.id
  from public.trip_members tm
 where s.to_member_id is null and tm.trip_id = s.trip_id
   and ((lower(s.to_person) = 'you' and tm.role = 'owner')
        or lower(coalesce(tm.guest_name, tm.display_name)) = lower(s.to_person));

do $$ begin
    if not exists (select 1 from pg_constraint where conname = 'trip_settlement_from_transaction_fk') then
        alter table public.trip_settlements add constraint trip_settlement_from_transaction_fk
            foreign key (from_transaction_id) references public.transactions(id) on delete set null;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'trip_settlement_to_transaction_fk') then
        alter table public.trip_settlements add constraint trip_settlement_to_transaction_fk
            foreign key (to_transaction_id) references public.transactions(id) on delete set null;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'trip_settlement_from_reversal_fk') then
        alter table public.trip_settlements add constraint trip_settlement_from_reversal_fk
            foreign key (from_reversal_transaction_id) references public.transactions(id) on delete set null;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'trip_settlement_to_reversal_fk') then
        alter table public.trip_settlements add constraint trip_settlement_to_reversal_fk
            foreign key (to_reversal_transaction_id) references public.transactions(id) on delete set null;
    end if;
    if not exists (select 1 from pg_constraint where conname = 'trip_transactions_settlement_fk') then
        alter table public.transactions add constraint trip_transactions_settlement_fk
            foreign key (trip_settlement_id) references public.trip_settlements(id) on delete set null;
    end if;
end $$;
create index if not exists trip_settlements_from_member_idx
    on public.trip_settlements (from_member_id, status, trip_id);
create index if not exists trip_settlements_to_member_idx
    on public.trip_settlements (to_member_id, status, trip_id);

create table if not exists public.trip_settlement_events (
    id bigserial primary key,
    settlement_id bigint not null references public.trip_settlements(id) on delete cascade,
    actor_user_id uuid references auth.users(id) on delete set null,
    event_type text not null check (event_type in ('created', 'paid', 'undone', 'received')),
    transaction_id integer references public.transactions(id) on delete set null,
    source_id integer references public.user_sources(id) on delete set null,
    created_at timestamptz not null default now()
);
create index if not exists trip_settlement_events_settlement_idx
    on public.trip_settlement_events (settlement_id, created_at desc);

-- RLS helper functions run with the table owner's privileges to avoid policy
-- recursion between trips and trip_members. They only answer membership.
create or replace function public.is_trip_member(p_trip_id integer, p_user_id uuid default auth.uid())
returns boolean language sql stable security definer set search_path = public, pg_temp as $$
    select exists (
        select 1 from public.trip_members tm
         where tm.trip_id = p_trip_id and tm.user_id = p_user_id and tm.active
    );
$$;
create or replace function public.is_trip_owner(p_trip_id integer, p_user_id uuid default auth.uid())
returns boolean language sql stable security definer set search_path = public, pg_temp as $$
    select exists (
        select 1 from public.trips t
         where t.id = p_trip_id and t.user_id = p_user_id
    );
$$;
create or replace function public.current_trip_member_id(p_trip_id integer)
returns bigint language sql stable security definer set search_path = public, pg_temp as $$
    select tm.id from public.trip_members tm
     where tm.trip_id=p_trip_id and tm.user_id=auth.uid() and tm.active
     order by (tm.role='owner') desc, tm.id limit 1;
$$;
create or replace function public.current_trip_expense_permissions(p_trip_id integer)
returns table(expense_id integer, is_author boolean, payment_transaction_id integer)
language sql stable security definer set search_path = public, pg_temp as $$
    select e.id, e.user_id=auth.uid(),
           case when e.user_id=auth.uid() then e.payment_transaction_id else null end
      from public.trip_expenses e
     where e.trip_id=p_trip_id and public.is_trip_member(p_trip_id,auth.uid());
$$;
create or replace function public.current_trip_share_links(p_trip_id integer)
returns table(share_id bigint, personal_transaction_id integer, payer_transaction_id integer)
language sql stable security definer set search_path = public, pg_temp as $$
    select s.id,
           case when tm.user_id=auth.uid() then s.personal_transaction_id else null end,
           case when e.user_id=auth.uid() then s.payer_transaction_id else null end
      from public.trip_expense_shares s
      join public.trip_expenses e on e.id=s.trip_expense_id
      join public.trip_members tm on tm.id=s.trip_member_id
     where e.trip_id=p_trip_id and public.is_trip_member(p_trip_id,auth.uid());
$$;
create or replace function public.current_trip_settlement_links(p_trip_id integer)
returns table(settlement_id bigint, from_transaction_id integer, to_transaction_id integer)
language sql stable security definer set search_path = public, pg_temp as $$
    select s.id,
           case when fm.user_id=auth.uid() then s.from_transaction_id else null end,
           case when tm.user_id=auth.uid() then s.to_transaction_id else null end
      from public.trip_settlements s
      left join public.trip_members fm on fm.id=s.from_member_id
      left join public.trip_members tm on tm.id=s.to_member_id
     where s.trip_id=p_trip_id and public.is_trip_member(p_trip_id,auth.uid());
$$;
create or replace function public.is_trip_expense_author(p_expense_id integer)
returns boolean language sql stable security definer set search_path = public, pg_temp as $$
    select exists (select 1 from public.trip_expenses e where e.id=p_expense_id and e.user_id=auth.uid());
$$;
create or replace function public.trip_is_mutable(p_trip_id integer)
returns boolean language sql stable security definer set search_path = public, pg_temp as $$
    select exists (
        select 1 from public.trips t
         where t.id = p_trip_id and t.status <> 'archived'
    );
$$;
revoke all on function public.is_trip_member(integer, uuid) from public;
revoke all on function public.is_trip_owner(integer, uuid) from public;
revoke all on function public.current_trip_member_id(integer) from public;
revoke all on function public.is_trip_expense_author(integer) from public;
revoke all on function public.trip_is_mutable(integer) from public;
grant execute on function public.is_trip_member(integer, uuid) to authenticated;
grant execute on function public.is_trip_owner(integer, uuid) to authenticated;
grant execute on function public.current_trip_member_id(integer) to authenticated;
grant execute on function public.is_trip_expense_author(integer) to authenticated;
grant execute on function public.trip_is_mutable(integer) to authenticated;

create or replace function public.create_trip_owner_member()
returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
begin
    insert into public.trip_members (trip_id, user_id, display_name, profile_emoji, role)
    select new.id, new.user_id,
           coalesce(nullif(btrim(p.display_name), ''), 'Minto member'), p.profile_emoji, 'owner'
      from public.profiles p where p.id = new.user_id
    on conflict do nothing;
    if not found then
        insert into public.trip_members (trip_id, user_id, display_name, role)
        values (new.id, new.user_id, 'Minto member', 'owner')
        on conflict do nothing;
    end if;
    return new;
end;
$$;
drop trigger if exists create_trip_owner_member on public.trips;
create trigger create_trip_owner_member after insert on public.trips
for each row execute function public.create_trip_owner_member();

-- Friend profile RPCs expose only public display fields, never emails or UUIDs.
create or replace function public.search_minto_users(p_query text)
returns table(username text, display_name text, profile_emoji text, relationship text)
language plpgsql stable security definer set search_path = public, pg_temp as $$
declare v_query text := lower(btrim(coalesce(p_query, '')));
begin
    if auth.uid() is null or v_query !~ '^[a-z0-9_]{2,24}$' then return; end if;
    return query
    select p.username,
           coalesce(nullif(btrim(p.display_name), ''), p.username),
           p.profile_emoji,
           case
             when exists (select 1 from public.friends f
                where f.user_a = least(auth.uid(), p.id) and f.user_b = greatest(auth.uid(), p.id)) then 'friend'
             when exists (select 1 from public.friend_requests r
                where r.sender_user_id = auth.uid() and r.receiver_user_id = p.id and r.status = 'pending') then 'outgoing'
             when exists (select 1 from public.friend_requests r
                where r.sender_user_id = p.id and r.receiver_user_id = auth.uid() and r.status = 'pending') then 'incoming'
             else 'none'
           end
      from public.profiles p
     where p.id <> auth.uid() and p.username is not null
       and left(lower(p.username), length(v_query)) = v_query
     order by p.username limit 20;
end;
$$;

create or replace function public.list_minto_friends()
returns table(username text, display_name text, profile_emoji text)
language sql stable security definer set search_path = public, pg_temp as $$
    select p.username, coalesce(nullif(btrim(p.display_name), ''), p.username), p.profile_emoji
      from public.friends f
      join public.profiles p on p.id = case when f.user_a = auth.uid() then f.user_b else f.user_a end
     where auth.uid() in (f.user_a, f.user_b)
     order by lower(coalesce(nullif(btrim(p.display_name), ''), p.username));
$$;

create or replace function public.list_minto_friend_requests(p_direction text)
returns table(request_id bigint, username text, display_name text, profile_emoji text, created_at timestamptz)
language plpgsql stable security definer set search_path = public, pg_temp as $$
begin
    if auth.uid() is null or p_direction not in ('incoming', 'outgoing') then return; end if;
    return query
    select r.id, p.username, coalesce(nullif(btrim(p.display_name), ''), p.username), p.profile_emoji, r.created_at
      from public.friend_requests r
      join public.profiles p on p.id = case when p_direction = 'incoming' then r.sender_user_id else r.receiver_user_id end
     where r.status = 'pending'
       and (case when p_direction = 'incoming' then r.receiver_user_id else r.sender_user_id end) = auth.uid()
     order by r.created_at desc;
end;
$$;

create or replace function public.send_friend_request(p_username text)
returns text language plpgsql security definer set search_path = public, pg_temp as $$
declare v_target uuid; v_request public.friend_requests%rowtype;
begin
    if auth.uid() is null then raise exception 'Sign in to add a friend.'; end if;
    if lower(btrim(coalesce(p_username, ''))) !~ '^[a-z0-9_]{3,24}$' then
        raise exception 'Enter a valid Minto username.';
    end if;
    select id into v_target from public.profiles where username = lower(btrim(p_username));
    if v_target is null then raise exception 'No Minto user found.'; end if;
    if v_target = auth.uid() then raise exception 'You cannot add yourself as a friend.'; end if;
    if exists (select 1 from public.friends f
        where f.user_a = least(auth.uid(), v_target) and f.user_b = greatest(auth.uid(), v_target)) then
        return 'friend';
    end if;
    select * into v_request from public.friend_requests r
     where r.status = 'pending'
       and least(r.sender_user_id, r.receiver_user_id) = least(auth.uid(), v_target)
       and greatest(r.sender_user_id, r.receiver_user_id) = greatest(auth.uid(), v_target)
     limit 1;
    if found then
        if v_request.sender_user_id = auth.uid() then return 'pending'; end if;
        return 'incoming';
    end if;
    insert into public.friend_requests (sender_user_id, receiver_user_id)
    values (auth.uid(), v_target);
    return 'sent';
end;
$$;

create or replace function public.respond_friend_request(p_request_id bigint, p_action text)
returns text language plpgsql security definer set search_path = public, pg_temp as $$
declare v_request public.friend_requests%rowtype;
begin
    if auth.uid() is null then raise exception 'Sign in to respond to friend requests.'; end if;
    if p_action not in ('accept', 'reject') then raise exception 'Choose accept or reject.'; end if;
    select * into v_request from public.friend_requests where id = p_request_id for update;
    if not found then raise exception 'That friend request was not found.'; end if;
    if v_request.receiver_user_id <> auth.uid() then raise exception 'You cannot respond to that request.'; end if;
    if v_request.status = 'accepted' then return 'accepted'; end if;
    if v_request.status = 'rejected' then return 'rejected'; end if;
    update public.friend_requests
       set status = case when p_action = 'accept' then 'accepted' else 'rejected' end,
           responded_at = now()
     where id = p_request_id;
    if p_action = 'accept' then
        insert into public.friends (user_a, user_b)
        values (least(v_request.sender_user_id, v_request.receiver_user_id),
                greatest(v_request.sender_user_id, v_request.receiver_user_id))
        on conflict do nothing;
        return 'accepted';
    end if;
    return 'rejected';
end;
$$;

create or replace function public.remove_minto_friend(p_username text)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
declare v_target uuid;
begin
    if auth.uid() is null then raise exception 'Sign in to manage friends.'; end if;
    select id into v_target from public.profiles where username = lower(btrim(coalesce(p_username, '')));
    if v_target is null or v_target = auth.uid() then return false; end if;
    delete from public.friends
     where user_a = least(auth.uid(), v_target) and user_b = greatest(auth.uid(), v_target);
    return found;
end;
$$;

create or replace function public.add_trip_minto_member(p_trip_id integer, p_username text)
returns bigint language plpgsql security definer set search_path = public, pg_temp as $$
declare v_target uuid; v_profile public.profiles%rowtype; v_member_id bigint;
begin
    if auth.uid() is null or not public.is_trip_owner(p_trip_id, auth.uid()) then
        raise exception 'Only the trip owner can add members.';
    end if;
    if not public.trip_is_mutable(p_trip_id) then raise exception 'Archived trips are read-only.'; end if;
    select id into v_target from public.profiles where username = lower(btrim(coalesce(p_username, '')));
    if v_target is null then raise exception 'No Minto user found.'; end if;
    if not exists (select 1 from public.friends f
        where f.user_a = least(auth.uid(), v_target) and f.user_b = greatest(auth.uid(), v_target)) then
        raise exception 'Add this person as a Minto friend first.';
    end if;
    select * into v_profile from public.profiles where id = v_target;
    insert into public.trip_members (trip_id, user_id, display_name, profile_emoji, role, active, removed_at)
    values (p_trip_id, v_target,
            coalesce(nullif(btrim(v_profile.display_name), ''), v_profile.username),
            v_profile.profile_emoji, 'member', true, null)
    on conflict (trip_id, user_id) where user_id is not null
    do update set active = true, removed_at = null
    returning id into v_member_id;
    update public.trips set is_group = true where id = p_trip_id;
    return v_member_id;
end;
$$;

-- All app-created transaction rows share one protected helper. The flag is
-- false for responsibility/receivable records and true only for real account
-- movements, which keeps balances separate from expense recognition.
create or replace function public.create_minto_transaction(
    p_user_id uuid, p_entry_text text, p_direction text, p_category text,
    p_amount numeric, p_source_id integer, p_description text,
    p_transaction_date date, p_counterparty text, p_share_id bigint,
    p_expense_id integer, p_settlement_id bigint, p_affects_balance boolean
) returns integer language plpgsql security definer set search_path = public, pg_temp as $$
declare v_entry_id integer;
begin
    if p_amount is null or p_amount < 0 then raise exception 'Transaction amount is invalid.'; end if;
    if p_direction not in ('in', 'out') then raise exception 'Transaction direction is invalid.'; end if;
    if p_source_id is not null and not exists (
        select 1 from public.user_sources s where s.id = p_source_id and s.user_id = p_user_id and s.active
    ) then raise exception 'Choose an active account that belongs to you.'; end if;
    insert into public.entries (user_id, entry_text, mode)
    values (p_user_id, left(coalesce(p_entry_text, p_description, 'Trip transaction'), 1000), 'manual')
    returning id into v_entry_id;
    insert into public.transactions (
        id, user_id, direction, category, source_id, amount, currency,
        description, raw_text, transaction_date, counterparty,
        trip_expense_id, trip_expense_share_id, trip_settlement_id,
        affects_source_balance, trip_share_status
    ) values (
        v_entry_id, p_user_id, p_direction, p_category, p_source_id, p_amount, 'INR',
        left(coalesce(p_description, 'Trip transaction'), 500),
        left(coalesce(p_entry_text, p_description, 'Trip transaction'), 1000),
        coalesce(p_transaction_date, current_date), nullif(btrim(p_counterparty), ''),
        p_expense_id, p_share_id, p_settlement_id, coalesce(p_affects_balance, true),
        case when p_share_id is null then null else 'pending' end
    );
    return v_entry_id;
end;
$$;

create or replace function public.create_shared_trip_expense(
    p_trip_id integer, p_description text, p_amount numeric, p_category text,
    p_expense_date date, p_payer_member_id bigint, p_source_id integer, p_shares jsonb
) returns integer language plpgsql security definer set search_path = public, pg_temp as $$
declare
    v_trip public.trips%rowtype;
    v_payer public.trip_members%rowtype;
    v_member public.trip_members%rowtype;
    v_expense_id integer;
    v_share_id bigint;
    v_txn_id integer;
    v_share jsonb;
    v_member_id bigint;
    v_share_amount numeric;
    v_total numeric := 0;
    v_status text;
    v_payer_name text;
begin
    if auth.uid() is null then raise exception 'Sign in to add a trip expense.'; end if;
    select * into v_trip from public.trips where id = p_trip_id;
    if not found or not public.is_trip_member(p_trip_id, auth.uid()) then raise exception 'That trip was not found.'; end if;
    if v_trip.status = 'archived' then raise exception 'Archived trips are read-only.'; end if;
    if nullif(btrim(p_description), '') is null or p_amount is null or p_amount <= 0
       or p_amount > 100000000 or p_amount <> round(p_amount, 2) then
        raise exception 'Enter a description and an amount greater than zero.';
    end if;
    if p_category not in ('food','fuel','stay','toll','parking','tickets','transport','shopping','entertainment','vehicle','medical','other') then
        raise exception 'Pick a valid expense category.';
    end if;
    select * into v_payer from public.trip_members
     where id = p_payer_member_id and trip_id = p_trip_id and active;
    if not found then raise exception 'Pick an active trip member who paid.'; end if;
    if v_payer.user_id is not null and v_payer.user_id <> auth.uid() then
        raise exception 'Only the Minto member who paid can link an expense to a personal account.';
    end if;
    if v_payer.user_id = auth.uid() then
        if p_source_id is null then raise exception 'Choose the account used to pay.'; end if;
        if not exists (select 1 from public.user_sources s where s.id=p_source_id and s.user_id=auth.uid() and s.active) then
            raise exception 'Choose one of your active accounts.';
        end if;
    elsif p_source_id is not null then
        raise exception 'A guest payment cannot use your personal account.';
    end if;
    if jsonb_typeof(p_shares) <> 'array' or jsonb_array_length(p_shares) = 0 then
        raise exception 'Add at least one trip member to the split.';
    end if;
    if (select count(*) from jsonb_array_elements(p_shares)) <>
       (select count(distinct (value->>'member_id')::bigint) from jsonb_array_elements(p_shares)) then
        raise exception 'Each member can appear once in the split.';
    end if;
    for v_share in select value from jsonb_array_elements(p_shares) loop
        v_member_id := (v_share->>'member_id')::bigint;
        v_share_amount := (v_share->>'amount')::numeric;
        if v_share_amount is null or v_share_amount <= 0 or v_share_amount <> round(v_share_amount, 2) then
            raise exception 'Share amounts must be positive rupee amounts with no fractions of a paise.';
        end if;
        if not exists (select 1 from public.trip_members tm
            where tm.id=v_member_id and tm.trip_id=p_trip_id and tm.active) then
            raise exception 'A split member does not belong to this trip.';
        end if;
        v_total := v_total + v_share_amount;
    end loop;
    if round(v_total, 2) <> round(p_amount, 2) then raise exception 'The split must add up to the expense total.'; end if;

    v_payer_name := case when v_payer.user_id = auth.uid() then 'You' else v_payer.display_name end;
    insert into public.trip_expenses (
        trip_id, user_id, description, amount, paid_by, expense_date, category,
        payer_member_id
    ) values (
        p_trip_id, auth.uid(), btrim(p_description), p_amount, v_payer_name,
        coalesce(p_expense_date, current_date), p_category, p_payer_member_id
    ) returning id into v_expense_id;

    for v_share in select value from jsonb_array_elements(p_shares) loop
        v_member_id := (v_share->>'member_id')::bigint;
        v_share_amount := (v_share->>'amount')::numeric;
        select * into v_member from public.trip_members where id=v_member_id and trip_id=p_trip_id;
        v_status := case when v_member_id = p_payer_member_id or v_member.user_id is null then 'accepted' else 'pending' end;
        insert into public.trip_expense_shares (trip_expense_id, trip_member_id, amount, status, responded_at)
        values (v_expense_id, v_member_id, v_share_amount, v_status,
                case when v_status = 'accepted' then now() else null end)
        returning id into v_share_id;
        insert into public.trip_expense_splits (trip_expense_id, user_id, participant_name, share_amount, trip_member_id)
        values (v_expense_id, auth.uid(),
                case when v_member.user_id = auth.uid() then 'You' else v_member.display_name end,
                v_share_amount, v_member_id);

        if v_payer.user_id = auth.uid() and v_member_id = p_payer_member_id then
            v_txn_id := public.create_minto_transaction(
                auth.uid(), 'Trip expense share: ' || btrim(p_description), 'out', 'expense',
                v_share_amount, p_source_id, btrim(p_description), coalesce(p_expense_date,current_date),
                null, v_share_id, v_expense_id, null, false
            );
            update public.trip_expense_shares set personal_transaction_id=v_txn_id where id=v_share_id;
            update public.transactions set trip_share_status='accepted', expense_category=p_category where id=v_txn_id;
        elsif v_payer.user_id = auth.uid() and v_member.user_id is null then
            v_txn_id := public.create_minto_transaction(
                auth.uid(), 'Trip amount advanced for ' || v_member.display_name, 'out', 'lending',
                v_share_amount, null, 'Trip amount advanced: ' || btrim(p_description),
                coalesce(p_expense_date,current_date), v_member.display_name,
                v_share_id, v_expense_id, null, false
            );
            update public.trip_expense_shares set payer_transaction_id=v_txn_id where id=v_share_id;
            update public.transactions set trip_share_status='accepted' where id=v_txn_id;
        end if;
    end loop;

    if v_payer.user_id = auth.uid() then
        v_txn_id := public.create_minto_transaction(
            auth.uid(), 'Trip payment: ' || btrim(p_description), 'out', 'trip_expense_payment',
            p_amount, p_source_id, 'Paid for ' || v_trip.name || ': ' || btrim(p_description),
            coalesce(p_expense_date,current_date), null, null, v_expense_id, null, true
        );
        update public.trip_expenses set payment_transaction_id=v_txn_id where id=v_expense_id;
    end if;
    return v_expense_id;
end;
$$;

-- Legacy trip entries have no account movements. Keep their existing edit
-- and delete behavior while preventing an edit from detaching accepted,
-- account-linked shares created by the new workflow.
create or replace function public.update_unlinked_trip_expense(
    p_expense_id integer, p_description text, p_amount numeric, p_category text,
    p_expense_date date, p_payer_member_id bigint, p_shares jsonb
) returns integer language plpgsql security definer set search_path = public, pg_temp as $$
declare
    v_expense public.trip_expenses%rowtype;
    v_trip public.trips%rowtype;
    v_member public.trip_members%rowtype;
    v_share jsonb;
    v_member_id bigint;
    v_share_amount numeric;
    v_total numeric := 0;
    v_share_status text;
begin
    if auth.uid() is null then raise exception 'Sign in to update this expense.'; end if;
    select * into v_expense from public.trip_expenses where id=p_expense_id for update;
    if not found or v_expense.user_id <> auth.uid() then raise exception 'That expense was not found.'; end if;
    select * into v_trip from public.trips where id=v_expense.trip_id;
    if v_trip.status='archived' then raise exception 'Archived trips are read-only.'; end if;
    if v_expense.payment_transaction_id is not null or exists (
        select 1 from public.trip_expense_shares s
         where s.trip_expense_id=p_expense_id
           and (s.personal_transaction_id is not null or s.payer_transaction_id is not null)
    ) then raise exception 'This expense has account-linked history and cannot be edited.'; end if;
    if nullif(btrim(p_description),'') is null or p_amount is null or p_amount<=0
       or p_amount>100000000 or p_amount<>round(p_amount,2) then
        raise exception 'Enter a description and an amount greater than zero.';
    end if;
    if p_category not in ('food','fuel','stay','toll','parking','tickets','transport','shopping','entertainment','vehicle','medical','other') then
        raise exception 'Pick a valid expense category.';
    end if;
    select * into v_member from public.trip_members
     where id=p_payer_member_id and trip_id=v_expense.trip_id and active;
    if not found or (v_member.user_id is not null and v_member.user_id<>auth.uid()) then
        raise exception 'Pick yourself or a guest as the payer.';
    end if;
    if jsonb_typeof(p_shares)<>'array' or jsonb_array_length(p_shares)=0 then
        raise exception 'Add at least one trip member to the split.';
    end if;
    if (select count(*) from jsonb_array_elements(p_shares)) <>
       (select count(distinct (value->>'member_id')::bigint) from jsonb_array_elements(p_shares)) then
        raise exception 'Each member can appear once in the split.';
    end if;
    for v_share in select value from jsonb_array_elements(p_shares) loop
        v_member_id := (v_share->>'member_id')::bigint;
        v_share_amount := (v_share->>'amount')::numeric;
        if v_share_amount is null or v_share_amount<=0 or v_share_amount<>round(v_share_amount,2) then
            raise exception 'Share amounts must be positive rupee amounts with no fractions of a paise.';
        end if;
        if not exists (select 1 from public.trip_members tm where tm.id=v_member_id and tm.trip_id=v_expense.trip_id and tm.active) then
            raise exception 'A split member does not belong to this trip.';
        end if;
        v_total := v_total + v_share_amount;
    end loop;
    if round(v_total,2)<>round(p_amount,2) then raise exception 'The split must add up to the expense total.'; end if;

    update public.trip_expenses set description=btrim(p_description), amount=p_amount,
        paid_by=case when v_member.user_id=auth.uid() then 'You' else v_member.display_name end,
        expense_date=coalesce(p_expense_date,current_date), category=p_category,
        payer_member_id=p_payer_member_id
     where id=p_expense_id;
    delete from public.trip_expense_splits where trip_expense_id=p_expense_id;
    delete from public.trip_expense_shares where trip_expense_id=p_expense_id;
    for v_share in select value from jsonb_array_elements(p_shares) loop
        v_member_id := (v_share->>'member_id')::bigint;
        v_share_amount := (v_share->>'amount')::numeric;
        select * into v_member from public.trip_members where id=v_member_id;
        v_share_status := case when v_member_id=p_payer_member_id or v_member.user_id is null then 'accepted' else 'pending' end;
        insert into public.trip_expense_shares (trip_expense_id,trip_member_id,amount,status,responded_at)
        values (p_expense_id,v_member_id,v_share_amount,v_share_status,
                case when v_share_status='accepted' then now() else null end);
        insert into public.trip_expense_splits (trip_expense_id,user_id,participant_name,share_amount,trip_member_id)
        values (p_expense_id,auth.uid(),case when v_member.user_id=auth.uid() then 'You' else v_member.display_name end,v_share_amount,v_member_id);
    end loop;
    return p_expense_id;
end;
$$;

create or replace function public.delete_unlinked_trip_expense(p_expense_id integer)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
declare v_expense public.trip_expenses%rowtype; v_trip public.trips%rowtype;
begin
    if auth.uid() is null then raise exception 'Sign in to delete this expense.'; end if;
    select * into v_expense from public.trip_expenses where id=p_expense_id for update;
    if not found or v_expense.user_id<>auth.uid() then raise exception 'That expense was not found.'; end if;
    select * into v_trip from public.trips where id=v_expense.trip_id;
    if v_trip.status='archived' then raise exception 'Archived trips are read-only.'; end if;
    if v_expense.payment_transaction_id is not null or exists (
        select 1 from public.trip_expense_shares s
         where s.trip_expense_id=p_expense_id
           and (s.personal_transaction_id is not null or s.payer_transaction_id is not null)
    ) then raise exception 'This expense has account-linked history and cannot be deleted.'; end if;
    delete from public.trip_expenses where id=p_expense_id;
    return true;
end;
$$;

create or replace function public.update_solo_trip_expense(
    p_expense_id integer, p_description text, p_amount numeric,
    p_category text, p_expense_date date
) returns integer language plpgsql security definer set search_path = public, pg_temp as $$
declare v_expense public.trip_expenses%rowtype; v_trip public.trips%rowtype;
begin
    if auth.uid() is null then raise exception 'Sign in to update this expense.'; end if;
    select * into v_expense from public.trip_expenses where id=p_expense_id for update;
    if not found or v_expense.user_id<>auth.uid() then raise exception 'That expense was not found.'; end if;
    select * into v_trip from public.trips where id=v_expense.trip_id;
    if v_trip.status='archived' then raise exception 'Archived trips are read-only.'; end if;
    if v_trip.is_group then raise exception 'Use the shared expense editor for this trip.'; end if;
    if nullif(btrim(p_description),'') is null or p_amount is null or p_amount<=0
       or p_amount>100000000 or p_amount<>round(p_amount,2) then
        raise exception 'Enter a description and an amount greater than zero.';
    end if;
    if p_category not in ('food','fuel','stay','toll','parking','tickets','transport','shopping','entertainment','vehicle','medical','other') then
        raise exception 'Pick a valid expense category.';
    end if;
    update public.trip_expenses set description=btrim(p_description), amount=p_amount,
        expense_date=coalesce(p_expense_date,current_date),category=p_category
     where id=p_expense_id;
    return p_expense_id;
end;
$$;

create or replace function public.accept_trip_expense_share(p_share_id bigint, p_source_id integer default null)
returns integer language plpgsql security definer set search_path = public, pg_temp as $$
declare
    v_share public.trip_expense_shares%rowtype;
    v_expense public.trip_expenses%rowtype;
    v_member public.trip_members%rowtype;
    v_trip public.trips%rowtype;
    v_source_id integer := p_source_id;
    v_txn_id integer;
    v_personal_txn_id integer;
begin
    if auth.uid() is null then raise exception 'Sign in to respond to this expense.'; end if;
    select s.* into v_share from public.trip_expense_shares s where s.id=p_share_id for update;
    if not found then raise exception 'That shared expense was not found.'; end if;
    select * into v_expense from public.trip_expenses where id=v_share.trip_expense_id;
    select * into v_trip from public.trips where id=v_expense.trip_id;
    select * into v_member from public.trip_members where id=v_share.trip_member_id;
    if v_member.user_id is distinct from auth.uid() then raise exception 'That expense share is not yours.'; end if;
    if v_trip.status = 'archived' then raise exception 'Archived trips are read-only.'; end if;
    if v_share.status in ('accepted','settled') then return v_share.personal_transaction_id; end if;
    if v_share.status = 'rejected' then raise exception 'This share was rejected. Ask the payer to reassign it.'; end if;
    if v_source_id is null then
        select default_expense_source_id into v_source_id from public.profiles where id=auth.uid();
    end if;
    if v_source_id is null then raise exception 'Choose an account for this personal expense.'; end if;
    v_txn_id := public.create_minto_transaction(
        auth.uid(), 'Trip share: ' || v_expense.description, 'out', 'expense',
        v_share.amount, v_source_id, v_expense.description, v_expense.expense_date,
        null, v_share.id, v_expense.id, null, false
    );
    update public.trip_expense_shares
       set status='accepted', personal_transaction_id=v_txn_id, responded_at=now()
     where id=v_share.id;
    update public.transactions set trip_share_status='accepted', expense_category=v_expense.category where id=v_txn_id;

    if exists (select 1 from public.trip_members pm
        where pm.id=v_expense.payer_member_id and pm.user_id=v_expense.user_id) then
        v_txn_id := public.create_minto_transaction(
            v_expense.user_id, 'Trip share accepted: ' || v_member.display_name, 'out', 'lending',
            v_share.amount, null, 'Trip amount advanced: ' || v_expense.description,
            v_expense.expense_date, v_member.display_name, v_share.id, v_expense.id, null, false
        );
        update public.trip_expense_shares set payer_transaction_id=v_txn_id where id=v_share.id;
        update public.transactions set trip_share_status='accepted' where id=v_txn_id;
    end if;
    v_personal_txn_id := (select personal_transaction_id from public.trip_expense_shares where id=v_share.id);
    return v_personal_txn_id;
end;
$$;

create or replace function public.reject_trip_expense_share(p_share_id bigint)
returns text language plpgsql security definer set search_path = public, pg_temp as $$
declare v_share public.trip_expense_shares%rowtype; v_member public.trip_members%rowtype; v_expense public.trip_expenses%rowtype;
begin
    if auth.uid() is null then raise exception 'Sign in to respond to this expense.'; end if;
    select * into v_share from public.trip_expense_shares where id=p_share_id for update;
    if not found then raise exception 'That shared expense was not found.'; end if;
    select * into v_member from public.trip_members where id=v_share.trip_member_id;
    select * into v_expense from public.trip_expenses where id=v_share.trip_expense_id;
    if v_member.user_id is distinct from auth.uid() then raise exception 'That expense share is not yours.'; end if;
    if not public.trip_is_mutable(v_expense.trip_id) then raise exception 'Archived trips are read-only.'; end if;
    if v_share.status = 'rejected' then return 'rejected'; end if;
    if v_share.status in ('accepted','settled') then raise exception 'An accepted expense cannot be rejected.'; end if;
    update public.trip_expense_shares set status='rejected', responded_at=now() where id=p_share_id;
    return 'rejected';
end;
$$;

create or replace function public.reassign_rejected_trip_share(p_share_id bigint, p_replacement_member_id bigint)
returns bigint language plpgsql security definer set search_path = public, pg_temp as $$
declare
    v_share public.trip_expense_shares%rowtype;
    v_expense public.trip_expenses%rowtype;
    v_old_member public.trip_members%rowtype;
    v_new_member public.trip_members%rowtype;
    v_new_share_id bigint;
    v_txn_id integer;
    v_payment_source integer;
begin
    if auth.uid() is null then raise exception 'Sign in to update this split.'; end if;
    select * into v_share from public.trip_expense_shares where id=p_share_id for update;
    if not found or v_share.status <> 'rejected' then raise exception 'Only a rejected share can be reassigned.'; end if;
    select * into v_expense from public.trip_expenses where id=v_share.trip_expense_id;
    if v_expense.user_id <> auth.uid() then raise exception 'Only the payer can reassign a rejected share.'; end if;
    if not public.trip_is_mutable(v_expense.trip_id) then raise exception 'Archived trips are read-only.'; end if;
    select * into v_old_member from public.trip_members where id=v_share.trip_member_id;
    select * into v_new_member from public.trip_members where id=p_replacement_member_id
      and trip_id=v_expense.trip_id and active;
    if not found or p_replacement_member_id=v_share.trip_member_id then raise exception 'Choose another active trip member.'; end if;
    if exists (select 1 from public.trip_expense_shares s
        where s.trip_expense_id=v_expense.id and s.trip_member_id=p_replacement_member_id and s.status <> 'rejected') then
        raise exception 'That member already has a share in this expense.';
    end if;
    select id into v_new_share_id from public.trip_expense_shares
     where trip_expense_id=v_expense.id and trip_member_id=p_replacement_member_id and status='rejected'
     limit 1;
    if v_new_share_id is null then
        insert into public.trip_expense_shares (trip_expense_id, trip_member_id, amount, status, responded_at)
        values (v_expense.id, p_replacement_member_id, v_share.amount,
                case when v_new_member.user_id is null or v_new_member.user_id=auth.uid() then 'accepted' else 'pending' end,
                case when v_new_member.user_id is null or v_new_member.user_id=auth.uid() then now() else null end)
        returning id into v_new_share_id;
    else
        update public.trip_expense_shares set amount=v_share.amount,
            status=case when v_new_member.user_id is null or v_new_member.user_id=auth.uid() then 'accepted' else 'pending' end,
            responded_at=case when v_new_member.user_id is null or v_new_member.user_id=auth.uid() then now() else null end,
            personal_transaction_id=null, payer_transaction_id=null
         where id=v_new_share_id;
    end if;
    delete from public.trip_expense_splits where trip_expense_id=v_expense.id and trip_member_id=p_replacement_member_id;
    insert into public.trip_expense_splits (trip_expense_id,user_id,participant_name,share_amount,trip_member_id)
    values (v_expense.id,auth.uid(),case when v_new_member.user_id=auth.uid() then 'You' else v_new_member.display_name end,
            v_share.amount,p_replacement_member_id);
    select tx.source_id into v_payment_source
      from public.trip_expenses e
      join public.transactions tx on tx.id=e.payment_transaction_id
     where e.id=v_expense.id;
    if v_new_member.user_id is null and exists (
        select 1 from public.trip_members payer
         where payer.id=v_expense.payer_member_id and payer.user_id=auth.uid()
    ) then
        v_txn_id := public.create_minto_transaction(
            auth.uid(), 'Trip amount advanced for ' || v_new_member.display_name, 'out', 'lending',
            v_share.amount, null, 'Trip amount advanced: ' || v_expense.description,
            v_expense.expense_date, v_new_member.display_name, v_new_share_id, v_expense.id, null, false
        );
        update public.trip_expense_shares set payer_transaction_id=v_txn_id where id=v_new_share_id;
        update public.transactions set trip_share_status='accepted' where id=v_txn_id;
    elsif v_new_member.user_id=auth.uid() then
        v_txn_id := public.create_minto_transaction(
            auth.uid(), 'Trip share: ' || v_expense.description, 'out', 'expense',
            v_share.amount, v_payment_source, v_expense.description, v_expense.expense_date,
            null, v_new_share_id, v_expense.id, null, false
        );
        update public.trip_expense_shares set personal_transaction_id=v_txn_id where id=v_new_share_id;
        update public.transactions set trip_share_status='accepted' where id=v_txn_id;
    end if;
    return v_new_share_id;
end;
$$;

create or replace function public.trip_member_net(p_trip_id integer, p_member_id bigint)
returns numeric language plpgsql stable security definer set search_path = public, pg_temp as $$
declare v_paid numeric; v_owed numeric; v_settled_from numeric; v_settled_to numeric;
begin
    select coalesce(sum(amount),0) into v_paid from public.trip_expenses
     where trip_id=p_trip_id and payer_member_id=p_member_id;
    select coalesce(sum(amount),0) into v_owed from public.trip_expense_shares s
     join public.trip_expenses e on e.id=s.trip_expense_id
     where e.trip_id=p_trip_id and s.trip_member_id=p_member_id and s.status in ('accepted','settled');
    select coalesce(sum(amount),0) into v_settled_from from public.trip_settlements
     where trip_id=p_trip_id and from_member_id=p_member_id and status='paid';
    select coalesce(sum(amount),0) into v_settled_to from public.trip_settlements
     where trip_id=p_trip_id and to_member_id=p_member_id and status='paid';
    return v_paid - v_owed + v_settled_from - v_settled_to;
end;
$$;

create or replace function public.current_trip_payables()
returns numeric language sql stable security definer set search_path = public, pg_temp as $$
    with owed as (
        select tm.id as member_id,
               coalesce(sum(s.amount) filter (where e.payer_member_id is distinct from tm.id),0) as amount
          from public.trip_members tm
          left join public.trip_expense_shares s on s.trip_member_id=tm.id and s.status in ('accepted','settled')
          left join public.trip_expenses e on e.id=s.trip_expense_id
         where tm.user_id=auth.uid()
         group by tm.id
    ), paid as (
        select from_member_id as member_id, coalesce(sum(amount),0) as amount
          from public.trip_settlements
         where status='paid'
         group by from_member_id
    )
    select coalesce(sum(greatest(owed.amount-coalesce(paid.amount,0),0)),0)
      from owed left join paid using (member_id);
$$;

create or replace function public.create_trip_settlement(
    p_trip_id integer, p_from_member_id bigint, p_to_member_id bigint,
    p_amount numeric, p_settlement_date date
) returns bigint language plpgsql security definer set search_path = public, pg_temp as $$
declare
    v_from public.trip_members%rowtype;
    v_to public.trip_members%rowtype;
    v_trip public.trips%rowtype;
    v_id bigint;
    v_pending_from numeric;
    v_pending_to numeric;
    v_available numeric;
begin
    if auth.uid() is null then raise exception 'Sign in to record a settlement.'; end if;
    select * into v_trip from public.trips where id=p_trip_id;
    if not found or not public.is_trip_member(p_trip_id,auth.uid()) then raise exception 'That trip was not found.'; end if;
    if v_trip.status='archived' then raise exception 'Archived trips are read-only.'; end if;
    select * into v_from from public.trip_members where id=p_from_member_id and trip_id=p_trip_id and active;
    select * into v_to from public.trip_members where id=p_to_member_id and trip_id=p_trip_id and active;
    if v_from.id is null or v_to.id is null or v_from.id=v_to.id then raise exception 'Pick two different active trip members.'; end if;
    if auth.uid() is distinct from v_from.user_id
       and auth.uid() is distinct from v_to.user_id
       and not public.is_trip_owner(p_trip_id,auth.uid()) then
        raise exception 'Only the people involved can record this settlement.';
    end if;
    if p_amount is null or p_amount <= 0 or p_amount > 100000000 or p_amount <> round(p_amount, 2) then
        raise exception 'Enter a valid settlement amount.';
    end if;
    select coalesce(sum(amount),0) into v_pending_from from public.trip_settlements
     where trip_id=p_trip_id and from_member_id=p_from_member_id and status='pending';
    select coalesce(sum(amount),0) into v_pending_to from public.trip_settlements
     where trip_id=p_trip_id and to_member_id=p_to_member_id and status='pending';
    v_available := least(
        -public.trip_member_net(p_trip_id,p_from_member_id)-v_pending_from,
        public.trip_member_net(p_trip_id,p_to_member_id)-v_pending_to
    );
    if v_available <= 0 or p_amount > v_available then
        raise exception 'That amount is larger than the current balance between these members.';
    end if;
    insert into public.trip_settlements (
        trip_id,user_id,from_person,to_person,amount,settlement_date,status,
        from_member_id,to_member_id
    ) values (
        p_trip_id,auth.uid(),v_from.display_name,v_to.display_name,p_amount,
        coalesce(p_settlement_date,current_date),'pending',p_from_member_id,p_to_member_id
    ) returning id into v_id;
    insert into public.trip_settlement_events (settlement_id,actor_user_id,event_type)
    values (v_id,auth.uid(),'created');
    return v_id;
end;
$$;

create or replace function public.set_trip_settlement_paid(
    p_settlement_id bigint, p_source_id integer, p_new_status text
) returns text language plpgsql security definer set search_path = public, pg_temp as $$
declare
    v_settlement public.trip_settlements%rowtype;
    v_from public.trip_members%rowtype;
    v_to public.trip_members%rowtype;
    v_trip public.trips%rowtype;
    v_txn_id integer;
begin
    if auth.uid() is null then raise exception 'Sign in to update the settlement.'; end if;
    if p_new_status not in ('paid','pending') then raise exception 'Choose paid or pending.'; end if;
    select * into v_settlement from public.trip_settlements where id=p_settlement_id for update;
    if not found then raise exception 'That settlement was not found.'; end if;
    select * into v_trip from public.trips where id=v_settlement.trip_id;
    select * into v_from from public.trip_members where id=v_settlement.from_member_id;
    select * into v_to from public.trip_members where id=v_settlement.to_member_id;
    if not public.is_trip_member(v_settlement.trip_id,auth.uid()) then raise exception 'That settlement is not available to you.'; end if;
    if v_trip.status='archived' then raise exception 'Archived trips are read-only.'; end if;
    if p_new_status=v_settlement.status then return p_new_status; end if;

    if p_new_status='paid' then
        if v_from.user_id is not null and v_from.user_id <> auth.uid() then raise exception 'Only the person paying can mark this paid.'; end if;
        if v_from.user_id = auth.uid() then
            if p_source_id is null then raise exception 'Choose the account used for this payment.'; end if;
            v_txn_id := public.create_minto_transaction(
                auth.uid(), 'Trip settlement paid to ' || v_to.display_name, 'out', 'trip_settlement',
                v_settlement.amount, p_source_id, 'Trip settlement: ' || v_from.display_name || ' to ' || v_to.display_name,
                v_settlement.settlement_date, v_to.display_name, null, null, v_settlement.id, true
            );
            update public.trip_settlements set from_source_id=p_source_id, from_transaction_id=v_txn_id,
                from_reversal_transaction_id=null, status='paid', paid_at=now() where id=v_settlement.id;
            insert into public.trip_settlement_events (settlement_id,actor_user_id,event_type,transaction_id,source_id)
            values (v_settlement.id,auth.uid(),'paid',v_txn_id,p_source_id);
        else
            -- Guest settlement: preserve the old name-only ledger behavior.
            if not public.is_trip_owner(v_settlement.trip_id,auth.uid()) then raise exception 'Only the trip owner can record a guest payment.'; end if;
            update public.trip_settlements set status='paid', paid_at=now() where id=v_settlement.id;
            insert into public.trip_settlement_events (settlement_id,actor_user_id,event_type)
            values (v_settlement.id,auth.uid(),'paid');
        end if;
        return 'paid';
    end if;

    if v_settlement.to_transaction_id is not null then
        if v_to.user_id is distinct from auth.uid() then
            raise exception 'The recipient must undo the receipt before the payer can undo this settlement.';
        end if;
        v_txn_id := public.create_minto_transaction(
            v_to.user_id, 'Undo trip settlement received from ' || v_from.display_name, 'out', 'trip_settlement',
            v_settlement.amount, v_settlement.to_source_id, 'Undo trip settlement receipt',
            current_date, v_from.display_name, null, null, v_settlement.id, true
        );
        update public.trip_settlements set to_reversal_transaction_id=v_txn_id,
            received_at=null, to_transaction_id=null, to_source_id=null where id=v_settlement.id;
        perform public.create_minto_transaction(
            v_to.user_id, 'Restore trip amount owed by ' || v_from.display_name, 'out', 'lending',
            v_settlement.amount, null, 'Restore trip receivable after settlement receipt undo',
            current_date, v_from.display_name, null, null, v_settlement.id, false
        );
        insert into public.trip_settlement_events (settlement_id,actor_user_id,event_type,transaction_id,source_id)
        values (v_settlement.id,auth.uid(),'undone',v_txn_id,v_settlement.to_source_id);
        return 'paid';
    end if;
    if (v_from.user_id is not null and v_from.user_id is distinct from auth.uid())
       or (v_from.user_id is null and not public.is_trip_owner(v_settlement.trip_id,auth.uid())) then
        raise exception 'Only the payer or trip owner can undo a settlement.';
    end if;
    if v_settlement.from_transaction_id is not null and v_from.user_id=auth.uid() then
        v_txn_id := public.create_minto_transaction(
            auth.uid(), 'Undo trip settlement to ' || v_to.display_name, 'in', 'trip_settlement',
            v_settlement.amount, v_settlement.from_source_id, 'Undo trip settlement: ' || v_from.display_name || ' to ' || v_to.display_name,
            current_date, v_to.display_name, null, null, v_settlement.id, true
        );
        update public.trip_settlements set from_reversal_transaction_id=v_txn_id where id=v_settlement.id;
        insert into public.trip_settlement_events (settlement_id,actor_user_id,event_type,transaction_id,source_id)
        values (v_settlement.id,auth.uid(),'undone',v_txn_id,v_settlement.from_source_id);
    else
        insert into public.trip_settlement_events (settlement_id,actor_user_id,event_type)
        values (v_settlement.id,auth.uid(),'undone');
    end if;
    update public.trip_settlements set status='pending', paid_at=null, from_source_id=null
     where id=v_settlement.id;
    return 'pending';
end;
$$;

create or replace function public.record_trip_settlement_receipt(p_settlement_id bigint, p_source_id integer)
returns integer language plpgsql security definer set search_path = public, pg_temp as $$
declare v_settlement public.trip_settlements%rowtype; v_from public.trip_members%rowtype; v_to public.trip_members%rowtype; v_txn_id integer;
begin
    if auth.uid() is null then raise exception 'Sign in to record the receipt.'; end if;
    select * into v_settlement from public.trip_settlements where id=p_settlement_id for update;
    if not found or v_settlement.status <> 'paid' then raise exception 'Mark the payment paid before recording receipt.'; end if;
    select * into v_from from public.trip_members where id=v_settlement.from_member_id;
    select * into v_to from public.trip_members where id=v_settlement.to_member_id;
    if v_to.user_id is distinct from auth.uid() then raise exception 'Only the recipient can record this receipt.'; end if;
    if p_source_id is null then raise exception 'Choose an account to receive the payment.'; end if;
    if v_settlement.to_transaction_id is not null then return v_settlement.to_transaction_id; end if;
    v_txn_id := public.create_minto_transaction(
        auth.uid(), 'Trip settlement received from ' || v_from.display_name, 'in', 'trip_settlement',
        v_settlement.amount, p_source_id, 'Trip settlement: ' || v_from.display_name || ' to ' || v_to.display_name,
        v_settlement.settlement_date, v_from.display_name, null, null, v_settlement.id, true
    );
    perform public.create_minto_transaction(
        auth.uid(), 'Trip receivable paid by ' || v_from.display_name, 'in', 'lending',
        v_settlement.amount, null, 'Trip amount repaid: ' || v_from.display_name,
        v_settlement.settlement_date, v_from.display_name, null, null, v_settlement.id, false
    );
    update public.trip_settlements set to_source_id=p_source_id,to_transaction_id=v_txn_id,received_at=now()
     where id=v_settlement.id;
    insert into public.trip_settlement_events (settlement_id,actor_user_id,event_type,transaction_id,source_id)
    values (v_settlement.id,auth.uid(),'received',v_txn_id,p_source_id);
    return v_txn_id;
end;
$$;

-- RLS: trip data is visible to active trip members; only authorized routes or
-- security-definer RPCs can create/alter cross-user relationships and shares.
alter table public.friend_requests enable row level security;
alter table public.friends enable row level security;
alter table public.trip_members enable row level security;
alter table public.trip_expense_shares enable row level security;
alter table public.trip_settlement_events enable row level security;
alter table public.trip_expense_splits enable row level security;

-- Replace the previous owner-only FOR ALL policies. Leaving them in place
-- would OR around the member policies and permit archived-row writes.
drop policy if exists "own trips" on public.trips;
drop policy if exists "own trip expenses" on public.trip_expenses;
drop policy if exists "own trip settlements" on public.trip_settlements;

drop policy if exists "friend requests visible to participants" on public.friend_requests;
create policy "friend requests visible to participants" on public.friend_requests
    for select using (auth.uid() in (sender_user_id, receiver_user_id));
drop policy if exists "friend pairs visible to participants" on public.friends;
create policy "friend pairs visible to participants" on public.friends
    for select using (auth.uid() in (user_a, user_b));

drop policy if exists "trips visible to active members" on public.trips;
create policy "trips visible to active members" on public.trips
    for select using (public.is_trip_member(id, auth.uid()));
drop policy if exists "trip owners create trips" on public.trips;
create policy "trip owners create trips" on public.trips
    for insert with check (auth.uid() = user_id);
drop policy if exists "trip owners update mutable trips" on public.trips;
create policy "trip owners update mutable trips" on public.trips
    for update using (auth.uid() = user_id and status <> 'archived')
    with check (auth.uid() = user_id);
drop policy if exists "trip owners delete mutable trips" on public.trips;
create policy "trip owners delete mutable trips" on public.trips
    for delete using (auth.uid() = user_id and status <> 'archived');

drop policy if exists "trip members visible to members" on public.trip_members;
create policy "trip members visible to members" on public.trip_members
    for select using (public.is_trip_member(trip_id, auth.uid()) or public.is_trip_owner(trip_id, auth.uid()));
drop policy if exists "trip owners add members" on public.trip_members;
create policy "trip owners add members" on public.trip_members
    for insert with check (public.is_trip_owner(trip_id,auth.uid()) and public.trip_is_mutable(trip_id)
                           and role='member' and user_id is null);
drop policy if exists "trip owners update members" on public.trip_members;
create policy "trip owners update members" on public.trip_members
    for update using (public.is_trip_owner(trip_id,auth.uid()) and public.trip_is_mutable(trip_id))
    with check (public.is_trip_owner(trip_id,auth.uid()));
drop policy if exists "trip owners remove members" on public.trip_members;
create policy "trip owners remove members" on public.trip_members
    for delete using (public.is_trip_owner(trip_id,auth.uid()) and public.trip_is_mutable(trip_id));

drop policy if exists "trip expenses visible to members" on public.trip_expenses;
create policy "trip expenses visible to members" on public.trip_expenses
    for select using (public.is_trip_member(trip_id,auth.uid()));
drop policy if exists "members add trip expenses" on public.trip_expenses;
create policy "members add trip expenses" on public.trip_expenses
    for insert with check (user_id=auth.uid() and public.is_trip_member(trip_id,auth.uid()) and public.trip_is_mutable(trip_id));
drop policy if exists "authors update mutable trip expenses" on public.trip_expenses;
create policy "authors update mutable trip expenses" on public.trip_expenses
    for update using (user_id=auth.uid() and public.is_trip_member(trip_id,auth.uid()) and public.trip_is_mutable(trip_id))
    with check (user_id=auth.uid() and public.is_trip_member(trip_id,auth.uid()));
drop policy if exists "authors delete mutable trip expenses" on public.trip_expenses;
create policy "authors delete mutable trip expenses" on public.trip_expenses
    for delete using (user_id=auth.uid() and public.is_trip_member(trip_id,auth.uid()) and public.trip_is_mutable(trip_id));

drop policy if exists "trip expense shares visible to members" on public.trip_expense_shares;
create policy "trip expense shares visible to members" on public.trip_expense_shares
    for select using (exists (
        select 1 from public.trip_expenses e
         where e.id=trip_expense_id and public.is_trip_member(e.trip_id,auth.uid())
    ));

drop policy if exists "own trip expense splits" on public.trip_expense_splits;
drop policy if exists "trip expense splits visible to members" on public.trip_expense_splits;
create policy "trip expense splits visible to members" on public.trip_expense_splits
    for select using (exists (
        select 1 from public.trip_expenses e
         where e.id=trip_expense_id and public.is_trip_member(e.trip_id,auth.uid())
    ));
drop policy if exists "trip expense split author writes" on public.trip_expense_splits;
create policy "trip expense split author writes" on public.trip_expense_splits
    for all using (user_id=auth.uid() and exists (
        select 1 from public.trip_expenses e
         where e.id=trip_expense_id and e.user_id=auth.uid()
           and public.is_trip_member(e.trip_id,auth.uid()) and public.trip_is_mutable(e.trip_id)
    )) with check (user_id=auth.uid() and exists (
        select 1 from public.trip_expenses e
         where e.id=trip_expense_id and e.user_id=auth.uid()
           and public.is_trip_member(e.trip_id,auth.uid()) and public.trip_is_mutable(e.trip_id)
    ));

drop policy if exists "trip settlements visible to members" on public.trip_settlements;
create policy "trip settlements visible to members" on public.trip_settlements
    for select using (public.is_trip_member(trip_id,auth.uid()));
drop policy if exists "trip settlement events visible to members" on public.trip_settlement_events;
create policy "trip settlement events visible to members" on public.trip_settlement_events
    for select using (exists (
        select 1 from public.trip_settlements s
         where s.id=settlement_id and public.is_trip_member(s.trip_id,auth.uid())
    ));

-- Limit PostgREST column access as well as row access. Members need stable
-- trip/member/share IDs for actions, but never another account's UUID or
-- private transaction/source references. Sensitive references are returned
-- only by account-scoped RPCs below.
revoke all privileges on table public.friend_requests, public.friends,
    public.trip_participants, public.trip_settlement_events from public, anon, authenticated;

revoke all privileges on table public.trips from public, anon, authenticated;
grant select (id, name, is_group, active, created_at, destination, start_date, end_date, budget, status)
    on public.trips to authenticated;
grant insert (user_id, name, is_group, active, destination, start_date, end_date, budget, status)
    on public.trips to authenticated;
grant update (name, active, destination, start_date, end_date, budget, status)
    on public.trips to authenticated;
grant delete on public.trips to authenticated;
grant usage, select on sequence public.trips_id_seq to authenticated;

revoke all privileges on table public.trip_members from public, anon, authenticated;
grant select (id, trip_id, guest_name, display_name, profile_emoji, role, active, removed_at, created_at)
    on public.trip_members to authenticated;
grant insert (trip_id, guest_name, display_name, role) on public.trip_members to authenticated;
grant update (active, removed_at) on public.trip_members to authenticated;
grant usage, select on sequence public.trip_members_id_seq to authenticated;

revoke all privileges on table public.trip_expenses from public, anon, authenticated;
grant select (id, trip_id, description, amount, paid_by, expense_date, created_at, category, payer_member_id)
    on public.trip_expenses to authenticated;
grant insert (trip_id, user_id, description, amount, paid_by, expense_date, category)
    on public.trip_expenses to authenticated;
grant usage, select on sequence public.trip_expenses_id_seq to authenticated;

revoke all privileges on table public.trip_expense_splits from public, anon, authenticated;
grant select (id, trip_expense_id, participant_name, share_amount, trip_member_id)
    on public.trip_expense_splits to authenticated;

revoke all privileges on table public.trip_expense_shares from public, anon, authenticated;
grant select (id, trip_expense_id, trip_member_id, amount, status, created_at, responded_at)
    on public.trip_expense_shares to authenticated;

revoke all privileges on table public.trip_settlements from public, anon, authenticated;
grant select (id, trip_id, from_person, to_person, amount, settlement_date, status, paid_at,
              created_at, from_member_id, to_member_id, received_at)
    on public.trip_settlements to authenticated;

revoke all on function public.search_minto_users(text) from public;
revoke all on function public.list_minto_friends() from public;
revoke all on function public.list_minto_friend_requests(text) from public;
revoke all on function public.send_friend_request(text) from public;
revoke all on function public.respond_friend_request(bigint,text) from public;
revoke all on function public.remove_minto_friend(text) from public;
revoke all on function public.add_trip_minto_member(integer,text) from public;
revoke all on function public.create_minto_transaction(uuid,text,text,text,numeric,integer,text,date,text,bigint,integer,bigint,boolean) from public;
revoke all on function public.create_shared_trip_expense(integer,text,numeric,text,date,bigint,integer,jsonb) from public;
revoke all on function public.update_unlinked_trip_expense(integer,text,numeric,text,date,bigint,jsonb) from public;
revoke all on function public.delete_unlinked_trip_expense(integer) from public;
revoke all on function public.update_solo_trip_expense(integer,text,numeric,text,date) from public;
revoke all on function public.accept_trip_expense_share(bigint,integer) from public;
revoke all on function public.reject_trip_expense_share(bigint) from public;
revoke all on function public.reassign_rejected_trip_share(bigint,bigint) from public;
revoke all on function public.trip_member_net(integer,bigint) from public;
revoke all on function public.current_trip_payables() from public;
revoke all on function public.current_trip_expense_permissions(integer) from public;
revoke all on function public.current_trip_share_links(integer) from public;
revoke all on function public.current_trip_settlement_links(integer) from public;
revoke all on function public.create_trip_settlement(integer,bigint,bigint,numeric,date) from public;
revoke all on function public.set_trip_settlement_paid(bigint,integer,text) from public;
revoke all on function public.record_trip_settlement_receipt(bigint,integer) from public;
grant execute on function public.search_minto_users(text) to authenticated;
grant execute on function public.list_minto_friends() to authenticated;
grant execute on function public.list_minto_friend_requests(text) to authenticated;
grant execute on function public.send_friend_request(text) to authenticated;
grant execute on function public.respond_friend_request(bigint,text) to authenticated;
grant execute on function public.remove_minto_friend(text) to authenticated;
grant execute on function public.add_trip_minto_member(integer,text) to authenticated;
grant execute on function public.create_shared_trip_expense(integer,text,numeric,text,date,bigint,integer,jsonb) to authenticated;
grant execute on function public.update_unlinked_trip_expense(integer,text,numeric,text,date,bigint,jsonb) to authenticated;
grant execute on function public.delete_unlinked_trip_expense(integer) to authenticated;
grant execute on function public.update_solo_trip_expense(integer,text,numeric,text,date) to authenticated;
grant execute on function public.accept_trip_expense_share(bigint,integer) to authenticated;
grant execute on function public.reject_trip_expense_share(bigint) to authenticated;
grant execute on function public.reassign_rejected_trip_share(bigint,bigint) to authenticated;
grant execute on function public.create_trip_settlement(integer,bigint,bigint,numeric,date) to authenticated;
grant execute on function public.set_trip_settlement_paid(bigint,integer,text) to authenticated;
grant execute on function public.record_trip_settlement_receipt(bigint,integer) to authenticated;
grant execute on function public.current_trip_payables() to authenticated;
grant execute on function public.current_trip_expense_permissions(integer) to authenticated;
grant execute on function public.current_trip_share_links(integer) to authenticated;
grant execute on function public.current_trip_settlement_links(integer) to authenticated;

commit;
