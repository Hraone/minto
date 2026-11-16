-- Trip expenses are trip-only until the owner explicitly links one to Personal mode.
-- Apply this migration after the existing Minto trip migrations.

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
        raise exception 'Only the Minto member who paid can add a linked personal expense.';
    end if;
    if p_source_id is not null then
        raise exception 'Personal account linking is a separate action. Add the trip expense first.';
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
        trip_id, user_id, description, amount, paid_by, expense_date, category, payer_member_id
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
    end loop;
    return v_expense_id;
end;
$$;

create or replace function public.link_trip_expense_to_personal(
    p_expense_id integer, p_source_id integer
) returns integer language plpgsql security definer set search_path = public, pg_temp as $$
declare
    v_expense public.trip_expenses%rowtype;
    v_trip public.trips%rowtype;
    v_member public.trip_members%rowtype;
    v_transaction_id integer;
begin
    if auth.uid() is null then raise exception 'Sign in to link this expense.'; end if;
    select * into v_expense from public.trip_expenses where id=p_expense_id for update;
    if not found or v_expense.user_id <> auth.uid() then
        raise exception 'Only the person who added this expense can link it to Personal mode.';
    end if;
    select * into v_trip from public.trips where id=v_expense.trip_id;
    if not found or not public.is_trip_member(v_expense.trip_id, auth.uid()) then
        raise exception 'That trip was not found.';
    end if;
    if v_trip.status='archived' then raise exception 'Archived trips are read-only.'; end if;
    if v_expense.payment_transaction_id is not null then
        raise exception 'This trip expense is already linked to Personal mode.';
    end if;
    if v_trip.is_group then
        select * into v_member from public.trip_members where id=v_expense.payer_member_id
          and trip_id=v_expense.trip_id and active;
        if not found or v_member.user_id is distinct from auth.uid() then
            raise exception 'Only link an expense to Personal mode when you were the payer.';
        end if;
    end if;
    if p_source_id is null or not exists (
        select 1 from public.user_sources s
         where s.id=p_source_id and s.user_id=auth.uid() and s.active
    ) then raise exception 'Choose an active personal account that belongs to you.'; end if;

    v_transaction_id := public.create_minto_transaction(
        auth.uid(),
        'Trip expense: ' || v_expense.description,
        'out', 'expense', v_expense.amount, p_source_id,
        'Trip: ' || v_trip.name || ' · ' || v_expense.description,
        v_expense.expense_date, null, null, v_expense.id, null, true
    );
    update public.transactions set expense_category=v_expense.category
     where id=v_transaction_id and user_id=auth.uid();
    update public.trip_expenses set payment_transaction_id=v_transaction_id
     where id=v_expense.id;
    return v_transaction_id;
end;
$$;

revoke all on function public.link_trip_expense_to_personal(integer, integer) from public, anon;
grant execute on function public.link_trip_expense_to_personal(integer, integer) to authenticated;
notify pgrst, 'reload schema';
