-- Allow the user who recorded an advance to remove that advance.
-- Advances are stored as paid trip_settlements rows with is_advance = true.
-- Deleting one removes only the advance record; no Personal transaction is involved.

create or replace function public.delete_trip_advance_received(
    p_trip_id integer,
    p_settlement_id bigint
) returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_advance public.trip_settlements%rowtype;
begin
    if auth.uid() is null then
        raise exception 'Sign in to delete an advance.';
    end if;

    select * into v_advance
      from public.trip_settlements
     where id = p_settlement_id
       and trip_id = p_trip_id
       and is_advance = true
     for update;

    if not found then
        raise exception 'That advance was not found.';
    end if;

    if v_advance.user_id <> auth.uid() then
        raise exception 'Only the person who recorded this advance can delete it.';
    end if;

    if not public.is_trip_member(p_trip_id, auth.uid()) then
        raise exception 'You are not a member of this trip.';
    end if;

    if exists (
        select 1
          from public.trips
         where id = p_trip_id
           and status = 'archived'
    ) then
        raise exception 'Archived trips are read-only.';
    end if;

    delete from public.trip_settlements
     where id = p_settlement_id
       and trip_id = p_trip_id
       and is_advance = true
       and user_id = auth.uid();
end;
$$;

revoke all on function public.delete_trip_advance_received(integer, bigint) from public, anon;
grant execute on function public.delete_trip_advance_received(integer, bigint) to authenticated;

notify pgrst, 'reload schema';
