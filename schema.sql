-- Run this in the Supabase SQL editor (Project -> SQL Editor -> New query)
-- Supabase already provides `auth.users` for login/password via Supabase Auth.
-- We only create the app-specific tables here, all referencing auth.users(id).

-- Per-user app settings/profile info that isn't part of auth itself
create table public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    default_mode text not null default 'ai' check (default_mode in ('ai', 'manual')),
    created_at timestamp with time zone default now()
);

-- Each user's own list of banks/cards, replacing the old hardcoded enum
create table public.user_sources (
    id serial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    source_type text not null check (source_type in ('savings', 'credit_card')),
    active boolean not null default true,
    created_at timestamp with time zone default now(),
    unique (user_id, name)
);

-- Raw entries, same idea as the Telegram bot's `entries` table
create table public.entries (
    id serial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    entry_text text not null,
    mode text not null check (mode in ('ai', 'manual')),
    created_at timestamp with time zone default now()
);

-- Parsed/confirmed transactions
create table public.transactions (
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

-- Row Level Security: each user can only see/touch their own rows
alter table public.profiles enable row level security;
alter table public.user_sources enable row level security;
alter table public.entries enable row level security;
alter table public.transactions enable row level security;

create policy "own profile" on public.profiles
    for all using (auth.uid() = id);

create policy "own sources" on public.user_sources
    for all using (auth.uid() = user_id);

create policy "own entries" on public.entries
    for all using (auth.uid() = user_id);

create policy "own transactions" on public.transactions
    for all using (auth.uid() = user_id);
