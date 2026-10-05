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

-- Trip expense splitter (like a mini Splitwise) — deliberately its own
-- ledger, separate from user_sources/transactions. Paying for a group trip
-- and getting reimbursed isn't a bank transaction in the normal sense (only
-- part of what you paid was really "your" spending), so this tracks who
-- owes whom without touching your real balances or net worth.
create table if not exists public.trips (
    id serial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    is_group boolean not null default false,
    active boolean not null default true,
    created_at timestamp with time zone default now()
);

-- Friends on a group trip. Just names, the same lightweight way lending
-- tracks a counterparty — no real account needed for them to be "in" a trip.
-- "You" (the trip owner) is implicit and never stored as a row here.
create table if not exists public.trip_participants (
    id serial primary key,
    trip_id integer not null references public.trips(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    created_at timestamp with time zone default now(),
    unique (trip_id, name)
);

create table if not exists public.trip_expenses (
    id serial primary key,
    trip_id integer not null references public.trips(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    description text not null,
    amount numeric not null,
    paid_by text not null default 'You',
    expense_date date not null default current_date,
    created_at timestamp with time zone default now()
);

-- One row per participant per expense: how much of that expense is theirs.
-- Equal-split, equal-among-a-subset, and fully custom amounts all reduce to
-- the same shape here — only how these rows get generated differs.
create table if not exists public.trip_expense_splits (
    id serial primary key,
    trip_expense_id integer not null references public.trip_expenses(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    participant_name text not null,
    share_amount numeric not null
);

alter table public.trips enable row level security;
alter table public.trip_participants enable row level security;
alter table public.trip_expenses enable row level security;
alter table public.trip_expense_splits enable row level security;

drop policy if exists "own trips" on public.trips;
create policy "own trips" on public.trips
    for all using (auth.uid() = user_id);

drop policy if exists "own trip participants" on public.trip_participants;
create policy "own trip participants" on public.trip_participants
    for all using (auth.uid() = user_id);

drop policy if exists "own trip expenses" on public.trip_expenses;
create policy "own trip expenses" on public.trip_expenses
    for all using (auth.uid() = user_id);

drop policy if exists "own trip expense splits" on public.trip_expense_splits;
create policy "own trip expense splits" on public.trip_expense_splits
    for all using (auth.uid() = user_id);

-- Which mode the user was last in (Personal or Trip), so logging back in
-- during a trip lands them straight back in Trip mode.
alter table public.profiles add column if not exists app_mode text not null default 'personal' check (app_mode in ('personal', 'trip'));


-- Idempotency log for the monthly report email job. One row means that
-- a user's report for that calendar month was successfully emailed.
create table if not exists public.monthly_report_sends (
    id bigserial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    report_month date not null,
    sent_at timestamp with time zone not null default now(),
    unique (user_id, report_month)
);

alter table public.monthly_report_sends enable row level security;

drop policy if exists "own monthly report sends" on public.monthly_report_sends;
create policy "own monthly report sends" on public.monthly_report_sends
    for select using (auth.uid() = user_id);


-- ---------------------------------------------------------------------------
-- Profile preferences (name, theme, emoji, biometric login, card setup)
-- ---------------------------------------------------------------------------
alter table public.profiles add column if not exists display_name text;
alter table public.profiles add column if not exists theme text check (theme in ('light', 'dark'));
alter table public.profiles add column if not exists profile_emoji text;
alter table public.profiles add column if not exists biometric_enabled boolean not null default false;
alter table public.profiles add column if not exists credit_card_setup_completed boolean not null default false;

-- Card payments recorded as "for spending from before Minto was started"
alter table public.transactions add column if not exists is_previous_card_bill boolean not null default false;

-- ---------------------------------------------------------------------------
-- Fixed (monthly repeating) expenses
-- ---------------------------------------------------------------------------
create table if not exists public.fixed_expenses (
    id bigserial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    amount numeric not null check (amount > 0),
    due_day integer not null check (due_day between 1 and 31),
    category text not null default 'other',
    source_id integer references public.user_sources(id) on delete set null,
    active boolean not null default true,
    created_at timestamp with time zone not null default now()
);

create table if not exists public.fixed_expense_payments (
    id bigserial primary key,
    fixed_expense_id bigint not null references public.fixed_expenses(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    due_month date not null,
    transaction_id integer references public.transactions(id) on delete set null,
    paid_at timestamp with time zone not null default now(),
    -- One payment per expense per month, enforced by the database so a
    -- double tap can never count the same bill twice.
    unique (fixed_expense_id, due_month)
);

create index if not exists fixed_expense_payments_txn_idx on public.fixed_expense_payments (transaction_id);

alter table public.fixed_expenses enable row level security;
alter table public.fixed_expense_payments enable row level security;

drop policy if exists "own fixed expenses" on public.fixed_expenses;
create policy "own fixed expenses" on public.fixed_expenses
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "own fixed expense payments" on public.fixed_expense_payments;
create policy "own fixed expense payments" on public.fixed_expense_payments
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- Report download history (range and size only, never the report itself)
-- ---------------------------------------------------------------------------
create table if not exists public.report_history (
    id bigserial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    date_from date not null,
    date_to date not null,
    row_count integer not null default 0,
    file_format text not null default 'pdf',
    created_at timestamp with time zone not null default now()
);

create index if not exists report_history_user_idx on public.report_history (user_id, created_at desc);

alter table public.report_history enable row level security;

drop policy if exists "own report history" on public.report_history;
create policy "own report history" on public.report_history
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);


-- Fixed items can be a plain expense or an investment such as a SIP. Investments
-- are saved as investments (not expenses) when marked paid.
alter table public.fixed_expenses add column if not exists kind text not null default 'expense' check (kind in ('expense', 'investment'));
