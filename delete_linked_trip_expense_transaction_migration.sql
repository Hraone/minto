-- Allow a user to delete a linked trip expense and its Personal transaction together.
-- The trip expense is removed first; its share/split rows cascade, and the
-- linked transaction is then removed through entries so balance triggers run.
create or replace function public.delete_linked_trip_expense_transaction(p_transaction_id integer)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_expense_id integer;
    v_trip_id integer;
begin
    if auth.uid() is null then
        raise exception 'Sign in to delete this linked transaction.';
    end if;

    select e.id, e.trip_id
      into v_expense_id, v_trip_id
      from public.trip_expenses e
      join public.transactions t
        on t.trip_expense_id = e.id
       and t.id = p_transaction_id
     where t.user_id = auth.uid()
       and e.user_id = auth.uid()
       and e.payment_transaction_id = p_transaction_id
     for update of e, t;

    if not found then
        raise exception 'This transaction is not a linked trip expense you can delete.';
    end if;

    if exists (
        select 1 from public.trips tr
         where tr.id = v_trip_id and tr.status = 'archived'
    ) then
        raise exception 'Archived trips are read-only.';
    end if;

    delete from public.trip_expenses where id = v_expense_id;
    delete from public.entries where id = p_transaction_id and user_id = auth.uid();

    return true;
end;
$$;

revoke all on function public.delete_linked_trip_expense_transaction(integer) from public, anon;
grant execute on function public.delete_linked_trip_expense_transaction(integer) to authenticated;
notify pgrst, 'reload schema';
