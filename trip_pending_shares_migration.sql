-- Count Minto friend responsibility as soon as a share is assigned. Acceptance
-- records the friend's personal expense but must not create a second receivable.
-- Run once after minto_v2_migration.sql.
begin;

create or replace function public.trip_member_net(p_trip_id integer, p_member_id bigint)
returns numeric language plpgsql stable security definer set search_path = public, pg_temp as $$
declare v_paid numeric; v_owed numeric; v_settled_from numeric; v_settled_to numeric;
begin
    select coalesce(sum(amount),0) into v_paid from public.trip_expenses
     where trip_id=p_trip_id and payer_member_id=p_member_id;
    select coalesce(sum(amount),0) into v_owed from public.trip_expense_shares s
     join public.trip_expenses e on e.id=s.trip_expense_id
     where e.trip_id=p_trip_id and s.trip_member_id=p_member_id
       and s.status in ('pending','accepted','settled');
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
          left join public.trip_expense_shares s
            on s.trip_member_id=tm.id and s.status in ('pending','accepted','settled')
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

-- Pending Minto friend shares create an off-balance-sheet receivable for the
-- member who paid. Rejecting the share removes that unearned receivable.
create or replace function public.sync_pending_trip_share_receivable()
returns trigger language plpgsql security definer set search_path = public, pg_temp as $$
declare
    v_expense public.trip_expenses%rowtype;
    v_payer public.trip_members%rowtype;
    v_member public.trip_members%rowtype;
    v_transaction_id integer;
begin
    if tg_op = 'UPDATE' then
        if new.status='rejected' and old.status is distinct from 'rejected'
           and old.payer_transaction_id is not null then
            select * into v_expense from public.trip_expenses where id=new.trip_expense_id;
            delete from public.entries
             where id=old.payer_transaction_id and user_id=v_expense.user_id;
            update public.trip_expense_shares
               set payer_transaction_id=null
             where id=new.id and payer_transaction_id=old.payer_transaction_id;
            return new;
        end if;
        -- A rejected row reassigned to another Minto member needs a new
        -- receivable. Other updates are handled by their owning RPC.
        if old.status is distinct from 'rejected' or new.status <> 'pending' then
            return new;
        end if;
    end if;

    if new.status <> 'pending' or new.payer_transaction_id is not null then
        return new;
    end if;

    select * into v_expense from public.trip_expenses where id=new.trip_expense_id;
    select * into v_payer from public.trip_members where id=v_expense.payer_member_id;
    select * into v_member from public.trip_members where id=new.trip_member_id;

    -- Never create a ledger record for a user other than the expense author /
    -- authenticated payer, and leave guest shares on their existing path.
    if v_expense.user_id is null
       or v_payer.user_id is distinct from v_expense.user_id
       or v_member.user_id is null
       or v_member.user_id=v_payer.user_id then
        return new;
    end if;

    v_transaction_id := public.create_minto_transaction(
        v_payer.user_id,
        'Trip amount advanced for ' || v_member.display_name,
        'out', 'lending', new.amount, null,
        'Trip amount advanced: ' || v_expense.description,
        v_expense.expense_date, v_member.display_name,
        new.id, v_expense.id, null, false
    );
    update public.transactions set trip_share_status='pending'
     where id=v_transaction_id and user_id=v_payer.user_id;
    update public.trip_expense_shares set payer_transaction_id=v_transaction_id
     where id=new.id and payer_transaction_id is null;
    return new;
end;
$$;

revoke all on function public.sync_pending_trip_share_receivable() from public, anon, authenticated;
drop trigger if exists trip_share_pending_receivable_sync on public.trip_expense_shares;
create trigger trip_share_pending_receivable_sync
    after insert or update of status, amount on public.trip_expense_shares
    for each row execute function public.sync_pending_trip_share_receivable();

-- Backfill existing pending Minto shares where the expense author is also the
-- payer. The link check makes rerunning this migration safe.
do $$
declare
    v_share record;
    v_transaction_id integer;
begin
    for v_share in
        select s.id as share_id, s.trip_expense_id, s.amount,
               e.user_id as payer_user_id, e.description, e.expense_date,
               member.display_name as member_name
          from public.trip_expense_shares s
          join public.trip_expenses e on e.id=s.trip_expense_id
          join public.trip_members payer on payer.id=e.payer_member_id
          join public.trip_members member on member.id=s.trip_member_id
         where s.status='pending'
           and s.payer_transaction_id is null
           and payer.user_id=e.user_id
           and member.user_id is not null
           and member.user_id is distinct from payer.user_id
    loop
        v_transaction_id := public.create_minto_transaction(
            v_share.payer_user_id,
            'Trip amount advanced for ' || v_share.member_name,
            'out', 'lending', v_share.amount, null,
            'Trip amount advanced: ' || v_share.description,
            v_share.expense_date, v_share.member_name,
            v_share.share_id, v_share.trip_expense_id, null, false
        );
        update public.transactions set trip_share_status='pending'
         where id=v_transaction_id and user_id=v_share.payer_user_id;
        update public.trip_expense_shares set payer_transaction_id=v_transaction_id
         where id=v_share.share_id and payer_transaction_id is null;
    end loop;
end;
$$;

-- Acceptance converts the already-recorded pending receivable to accepted.
-- It creates a receivable only for legacy pending shares that have no link yet.
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
    if v_trip.status='archived' then raise exception 'Archived trips are read-only.'; end if;
    if v_share.status in ('accepted','settled') then return v_share.personal_transaction_id; end if;
    if v_share.status='rejected' then raise exception 'This share was rejected. Ask the payer to reassign it.'; end if;
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
    update public.transactions
       set trip_share_status='accepted', expense_category=v_expense.category
     where id=v_txn_id;

    if exists (select 1 from public.trip_members pm
        where pm.id=v_expense.payer_member_id and pm.user_id=v_expense.user_id) then
        if v_share.payer_transaction_id is not null then
            update public.transactions
               set trip_share_status='accepted'
             where id=v_share.payer_transaction_id
               and user_id=v_expense.user_id
               and trip_expense_share_id=v_share.id;
        end if;
        if v_share.payer_transaction_id is null or not found then
            v_txn_id := public.create_minto_transaction(
                v_expense.user_id, 'Trip amount advanced for ' || v_member.display_name,
                'out', 'lending', v_share.amount, null,
                'Trip amount advanced: ' || v_expense.description,
                v_expense.expense_date, v_member.display_name,
                v_share.id, v_expense.id, null, false
            );
            update public.trip_expense_shares set payer_transaction_id=v_txn_id where id=v_share.id;
            update public.transactions set trip_share_status='accepted' where id=v_txn_id;
        end if;
    end if;
    v_personal_txn_id := (select personal_transaction_id from public.trip_expense_shares where id=v_share.id);
    return v_personal_txn_id;
end;
$$;

revoke all on function public.trip_member_net(integer,bigint) from public;
revoke all on function public.current_trip_payables() from public;
revoke all on function public.accept_trip_expense_share(bigint,integer) from public;
revoke all on function public.trip_member_net(integer,bigint) from anon, authenticated;
revoke all on function public.current_trip_payables() from anon;
revoke all on function public.accept_trip_expense_share(bigint,integer) from anon;
grant execute on function public.current_trip_payables() to authenticated;
grant execute on function public.accept_trip_expense_share(bigint,integer) to authenticated;

comment on function public.sync_pending_trip_share_receivable() is
    'Creates/removes the payer receivable for an outstanding pending Minto friend share.';

commit;

