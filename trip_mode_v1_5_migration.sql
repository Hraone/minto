-- Minto Trip Mode V1.5 migration
-- Run this once against the existing Supabase database.

alter table public.trips add column if not exists destination text;
alter table public.trips add column if not exists start_date date;
alter table public.trips add column if not exists end_date date;
alter table public.trips add column if not exists budget numeric;
alter table public.trips add column if not exists status text not null default 'active';

alter table public.trips drop constraint if exists trips_status_check;
alter table public.trips add constraint trips_status_check
    check (status in ('planned', 'active', 'completed', 'archived'));

alter table public.trips drop constraint if exists trips_dates_check;
alter table public.trips add constraint trips_dates_check
    check (end_date is null or start_date is null or end_date >= start_date);

alter table public.trips drop constraint if exists trips_budget_check;
alter table public.trips add constraint trips_budget_check
    check (budget is null or budget >= 0);

alter table public.trip_expenses add column if not exists category text not null default 'other';

alter table public.trip_expenses drop constraint if exists trip_expenses_category_check;
alter table public.trip_expenses add constraint trip_expenses_category_check
    check (category in (
        'food', 'fuel', 'stay', 'toll', 'parking', 'tickets',
        'transport', 'shopping', 'entertainment', 'vehicle', 'medical', 'other'
    ));

alter table public.trip_expenses drop constraint if exists trip_expenses_amount_check;
alter table public.trip_expenses add constraint trip_expenses_amount_check
    check (amount > 0);

create index if not exists trips_status_idx on public.trips (user_id, status);
create index if not exists trips_dates_idx on public.trips (user_id, start_date, end_date);
create index if not exists trip_expenses_category_idx on public.trip_expenses (trip_id, category);

create table if not exists public.trip_settlements (
    id bigserial primary key,
    trip_id integer not null references public.trips(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    from_person text not null,
    to_person text not null,
    amount numeric not null check (amount > 0),
    settlement_date date not null default current_date,
    status text not null default 'pending',
    paid_at timestamp with time zone,
    created_at timestamp with time zone not null default now(),
    check (from_person <> to_person),
    check (status in ('pending', 'paid'))
);

create index if not exists trip_settlements_trip_idx
    on public.trip_settlements (trip_id, settlement_date desc);

alter table public.trip_settlements enable row level security;

drop policy if exists "own trip settlements" on public.trip_settlements;
create policy "own trip settlements" on public.trip_settlements
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
