-- Record an advance already received by the signed-in trip member.
-- It is stored as a paid trip settlement so it reduces the final balance,
-- without creating a Personal-mode transaction or requiring an expense first.

alter table public.trip_settlements
    add column if not exists is_advance boolean not null default false;

alter table public.trip_settlements
    add column if not exists note text;

grant select (is_advance, note) on public.trip_settlements to authenticated;

create or replace function public.record_trip_advance_received(
    p_trip_id integer,
    p_from_member_id bigint,
    p_amount numeric,
    p_advance_date date,
    p_note text default null
) returns bigint
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_trip public.trips%rowtype;
    v_from public.trip_members%rowtype;
    v_to public.trip_members%rowtype;
    v_id bigint;
begin
    if auth.uid() is null then
        raise exception 'Sign in to record an advance.';
    end if;

    select * into v_trip
      from public.trips
     where id = p_trip_id;
    if not found or not public.is_trip_member(p_trip_id, auth.uid()) then
        raise exception 'That trip was not found.';
    end if;
    if v_trip.status = 'archived' then
        raise exception 'Archived trips are read-only.';
    end if;

    select * into v_to
      from public.trip_members
     where trip_id = p_trip_id
       and user_id = auth.uid()
       and active
     order by id
     limit 1;
    if v_to.id is null then
        raise exception 'Your active trip membership was not found.';
    end if;

    select * into v_from
      from public.trip_members
     where id = p_from_member_id
       and trip_id = p_trip_id
       and active;
    if v_from.id is null or v_from.id = v_to.id then
        raise exception 'Choose another active trip participant.';
    end if;

    if p_amount is null or p_amount <= 0 or p_amount > 100000000
       or p_amount <> round(p_amount, 2) then
        raise exception 'Enter a valid advance amount.';
    end if;

    insert into public.trip_settlements (
        trip_id, user_id, from_person, to_person, amount, settlement_date,
        status, paid_at, received_at, from_member_id, to_member_id,
        is_advance, note
    ) values (
        p_trip_id, auth.uid(), v_from.display_name, v_to.display_name,
        p_amount, coalesce(p_advance_date, current_date),
        'paid', now(), now(), v_from.id, v_to.id,
        true, nullif(btrim(p_note), '')
    )
    returning id into v_id;

    return v_id;
end;
$$;

revoke all on function public.record_trip_advance_received(integer, bigint, numeric, date, text) from public, anon;
grant execute on function public.record_trip_advance_received(integer, bigint, numeric, date, text) to authenticated;

notify pgrst, 'reload schema';
