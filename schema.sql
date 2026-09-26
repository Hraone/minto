-- Run this in the Supabase SQL editor (Project -> SQL Editor -> New query)
-- Supabase already provides `auth.users` for login/password via Supabase Auth.
-- We only create the app-specific tables here, all referencing auth.users(id).
--
-- Safe to re-run: table creation uses IF NOT EXISTS and every column added
-- since the original version is added with ALTER TABLE ... ADD COLUMN IF NOT
-- EXISTS, so running this file again against your existing live database
-- will patch it up to date without erroring or touching existing data.

-- Per-user app settings/profile info that isn't part of auth itself
create table if not exists public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    default_mode text not null default 'ai' check (default_mode in ('ai', 'manual')),
    created_at timestamp with time zone default now()
);

-- Each user's own list of banks/cards, replacing the old hardcoded enum
create table if not exists public.user_sources (
    id serial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    source_type text not null check (source_type in ('savings', 'credit_card')),
    active boolean not null default true,
    created_at timestamp with time zone default now(),
    unique (user_id, name)
);

-- Savings-account fields (opening_balance also doubles as a credit card's
-- starting outstanding balance -- see compute_source_balances in app.py)
alter table public.user_sources add column if not exists opening_balance numeric not null default 0;
alter table public.user_sources add column if not exists minimum_balance numeric;
-- Credit-card-only field
alter table public.user_sources add column if not exists credit_limit numeric;

-- Raw entries, same idea as the Telegram bot's `entries` table
create table if not exists public.entries (
    id serial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    entry_text text not null,
    mode text not null check (mode in ('ai', 'manual')),
    created_at timestamp with time zone default now()
);

-- Parsed/confirmed transactions
create table if not exists public.transactions (
    id integer primary key references public.entries(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    direction text not null check (direction in ('in', 'out', 'unknown')),
    category text not null check (category in ('investment', 'expense', 'income', 'transfer', 'unknown')),
    expense_category text,
    source_id integer references public.user_sources(id),
    amount numeric,
    currency text,
    description text,
    raw_text text,
    created_at timestamp with time zone default now()
);

alter table public.transactions add column if not exists investment_category text;
-- Who the money was lent to / repaid by, for "lending" category entries only
-- (e.g. giving a friend money, or them paying you back). Lets outstanding
-- loans be tracked per person. Nullable — every other category ignores it.
alter table public.transactions add column if not exists counterparty text;

-- Widen the category check to include 'lending': money you hand to someone
-- else that you expect back. Structurally identical to 'investment' — an
-- 'out' leg reduces your cash but creates an asset (money owed to you), an
-- 'in' leg (repayment) reduces that asset back down. It is NOT 'transfer'
-- (that's reserved for moving money between your own sources) and NOT
-- 'expense' (you're not permanently out that money).
alter table public.transactions drop constraint if exists transactions_category_check;
alter table public.transactions add constraint transactions_category_check
    check (category in ('investment', 'expense', 'income', 'transfer', 'lending', 'unknown'));

-- Links the two legs of one transfer (e.g. a credit-card bill payment, or a
-- cash withdrawal: an "out" leg on the savings source and an "in" leg on the
-- card/cash source) so they can be found, shown, or deleted together.
-- Nullable — plain income/expense/investment entries don't use it.
alter table public.transactions add column if not exists transfer_group uuid;
create index if not exists transactions_transfer_group_idx on public.transactions (transfer_group);

-- Widen the source_type check to include 'cash': physical cash on hand,
-- which behaves exactly like a savings account for balance math (an
-- opening amount plus/minus flows) but is listed separately from bank
-- accounts on the Sources page since it isn't a bank.
alter table public.user_sources drop constraint if exists user_sources_source_type_check;
alter table public.user_sources add constraint user_sources_source_type_check
    check (source_type in ('savings', 'credit_card', 'cash'));

-- Per-user custom expense/investment categories, layered on top of the
-- fixed defaults every user starts with (see FIXED_EXPENSE_CATEGORIES /
-- FIXED_INVESTMENT_CATEGORIES in app.py). A user adding "Kids School" as an
-- expense category doesn't affect anyone else's category list.
create table if not exists public.user_categories (
    id serial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    kind text not null check (kind in ('expense', 'investment')),
    name text not null,
    created_at timestamp with time zone default now(),
    unique (user_id, kind, name)
);

-- Row Level Security: each user can only see/touch their own rows
alter table public.profiles enable row level security;
alter table public.user_sources enable row level security;
alter table public.entries enable row level security;
alter table public.transactions enable row level security;
alter table public.user_categories enable row level security;

drop policy if exists "own profile" on public.profiles;
create policy "own profile" on public.profiles
    for all using (auth.uid() = id);

drop policy if exists "own sources" on public.user_sources;
create policy "own sources" on public.user_sources
    for all using (auth.uid() = user_id);

drop policy if exists "own entries" on public.entries;
create policy "own entries" on public.entries
    for all using (auth.uid() = user_id);

drop policy if exists "own transactions" on public.transactions;
create policy "own transactions" on public.transactions
    for all using (auth.uid() = user_id);

drop policy if exists "own categories" on public.user_categories;
create policy "own categories" on public.user_categories
    for all using (auth.uid() = user_id);

-- The date the transaction actually happened, separate from created_at
-- (when it was typed into the app). Lets a backdated entry land in the
-- right period on the dashboard instead of always counting as "today".
alter table public.transactions add column if not exists transaction_date date;
update public.transactions set transaction_date = created_at::date where transaction_date is null;
alter table public.transactions alter column transaction_date set default current_date;
alter table public.transactions alter column transaction_date set not null;
