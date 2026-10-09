-- Run this once in Supabase SQL Editor to enable recurring fixed income.
-- Safe to re-run: all tables and indexes use IF NOT EXISTS and policies are replaced.

create table if not exists public.fixed_incomes (
    id bigserial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    amount numeric not null check (amount > 0),
    due_day integer not null check (due_day between 1 and 31),
    source_id integer references public.user_sources(id) on delete set null,
    active boolean not null default true,
    created_at timestamptz not null default now()
);

create table if not exists public.fixed_income_payments (
    id bigserial primary key,
    fixed_income_id bigint not null references public.fixed_incomes(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    due_month date not null,
    transaction_id integer references public.transactions(id) on delete set null,
    received_at timestamptz not null default now(),
    unique (fixed_income_id, due_month)
);

alter table public.fixed_incomes enable row level security;
alter table public.fixed_income_payments enable row level security;

drop policy if exists "own fixed incomes" on public.fixed_incomes;
create policy "own fixed incomes" on public.fixed_incomes
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "own fixed income payments" on public.fixed_income_payments;
create policy "own fixed income payments" on public.fixed_income_payments
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

create index if not exists fixed_income_payments_txn_idx
    on public.fixed_income_payments (transaction_id);
