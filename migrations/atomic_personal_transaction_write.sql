-- Make each personal entry + transaction save one atomic database call.
-- Apply after schema.sql and minto_v2_migration.sql.
begin;

alter table public.entries add column if not exists idempotency_key text;
create unique index if not exists entries_user_id_idempotency_key_uidx
    on public.entries (user_id, idempotency_key)
    where idempotency_key is not null;

alter table public.transactions add column if not exists transaction_date date;
alter table public.transactions add column if not exists transfer_group uuid;
alter table public.transactions add column if not exists is_previous_card_bill boolean not null default false;

create or replace function public.create_minto_personal_transaction(
    p_user_id uuid,
    p_entry_text text,
    p_transaction jsonb,
    p_idempotency_key text
) returns integer
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    v_auth_user_id uuid := auth.uid();
    v_entry_id integer;
    v_source_id integer;
    v_direction text;
    v_category text;
    v_expense_category text;
    v_investment_category text;
    v_counterparty text;
    v_amount numeric;
    v_currency text;
    v_description text;
    v_raw_text text;
    v_transaction_date date;
    v_transfer_group uuid;
    v_is_previous_card_bill boolean;
begin
    if v_auth_user_id is null or p_user_id is distinct from v_auth_user_id then
        raise exception 'Sign in to save your transaction.';
    end if;
    if p_transaction is null or jsonb_typeof(p_transaction) <> 'object' then
        raise exception 'Transaction details are missing.';
    end if;

    v_source_id := nullif(p_transaction ->> 'source_id', '')::integer;
    v_direction := p_transaction ->> 'direction';
    v_category := p_transaction ->> 'category';
    v_expense_category := p_transaction ->> 'expense_category';
    v_investment_category := p_transaction ->> 'investment_category';
    v_counterparty := p_transaction ->> 'counterparty';
    v_amount := nullif(p_transaction ->> 'amount', '')::numeric;
    v_currency := coalesce(nullif(p_transaction ->> 'currency', ''), 'INR');
    v_description := p_transaction ->> 'description';
    v_raw_text := p_transaction ->> 'raw_text';
    v_transaction_date := nullif(p_transaction ->> 'transaction_date', '')::date;
    v_transfer_group := nullif(p_transaction ->> 'transfer_group', '')::uuid;
    v_is_previous_card_bill := coalesce(
        nullif(p_transaction ->> 'is_previous_card_bill', '')::boolean,
        false
    );

    insert into public.entries (user_id, entry_text, mode, idempotency_key)
    values (
        v_auth_user_id,
        p_entry_text,
        'manual',
        nullif(btrim(p_idempotency_key), '')
    )
    on conflict (user_id, idempotency_key)
        where idempotency_key is not null
        do nothing
    returning id into v_entry_id;

    -- A retry for an already committed submission returns the original id.
    -- The entry and transaction are committed together, so no half-pair exists.
    if v_entry_id is null then
        select id into v_entry_id
        from public.entries
        where user_id = v_auth_user_id
          and idempotency_key = nullif(btrim(p_idempotency_key), '')
        limit 1;
        if v_entry_id is null then
            raise exception 'Could not resolve the saved transaction.';
        end if;
        return v_entry_id;
    end if;

    if v_source_id is not null and not exists (
        select 1
        from public.user_sources s
        where s.id = v_source_id
          and s.user_id = v_auth_user_id
          and s.active
    ) then
        raise exception 'Choose an active account that belongs to you.';
    end if;

    insert into public.transactions (
        id, user_id, direction, category, expense_category,
        investment_category, counterparty, source_id, amount, currency,
        description, raw_text, transaction_date, transfer_group,
        is_previous_card_bill
    ) values (
        v_entry_id, v_auth_user_id, v_direction, v_category, v_expense_category,
        v_investment_category, v_counterparty, v_source_id, v_amount, v_currency,
        v_description, v_raw_text, coalesce(v_transaction_date, current_date),
        v_transfer_group, v_is_previous_card_bill
    );

    return v_entry_id;
end;
$$;

revoke all on function public.create_minto_personal_transaction(uuid, text, jsonb, text) from public;
grant execute on function public.create_minto_personal_transaction(uuid, text, jsonb, text) to authenticated;

commit;
