-- Trip share acceptance / settlement accounting update.
-- Acceptance records responsibility only. The personal expense is created when
-- the member marks their settlement paid and selects the account used.

create or replace function public.accept_trip_expense_share(p_share_id bigint, p_source_id integer default null)
returns integer language plpgsql security definer set search_path = public, pg_temp as $$
declare
    v_share public.trip_expense_shares%rowtype;
    v_expense public.trip_expenses%rowtype;
    v_member public.trip_members%rowtype;
    v_trip public.trips%rowtype;
    v_txn_id integer;
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

    -- Do not create a personal expense or require an account at acceptance.
    update public.trip_expense_shares
       set status='accepted', personal_transaction_id=null, responded_at=now()
     where id=v_share.id;

    -- Keep the payer's receivable ledger entry when the payer is a Minto user.
    if exists (
        select 1 from public.trip_members pm
        where pm.id=v_expense.payer_member_id and pm.user_id=v_expense.user_id
    ) then
        v_txn_id := public.create_minto_transaction(
            v_expense.user_id, 'Trip share accepted: ' || v_member.display_name, 'out', 'lending',
            v_share.amount, null, 'Trip amount advanced: ' || v_expense.description,
            v_expense.expense_date, v_member.display_name, v_share.id, v_expense.id, null, false
        );
        update public.trip_expense_shares set payer_transaction_id=v_txn_id where id=v_share.id;
        update public.transactions set trip_share_status='accepted' where id=v_txn_id;
    end if;
    return null;
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
            -- Record the real account movement as a personal expense only now,
            -- after the payer explicitly marks settlement paid and picks an account.
            v_txn_id := public.create_minto_transaction(
                auth.uid(), 'Trip share settlement paid to ' || v_to.display_name, 'out', 'expense',
                v_settlement.amount, p_source_id, 'Trip settlement: ' || v_from.display_name || ' to ' || v_to.display_name,
                v_settlement.settlement_date, v_to.display_name, null, null, v_settlement.id, true
            );
            update public.transactions
               set expense_category='other'
             where id=v_txn_id and user_id=auth.uid();
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
        -- Hide the original expense again when undoing the settlement, while
        -- preserving the source-balance reversal transaction.
        update public.transactions
           set category='trip_settlement', expense_category=null
         where id=v_settlement.from_transaction_id and user_id=auth.uid();
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
