-- Daily expense summary / reminder Web Push support.
-- Run this migration in the Supabase SQL editor before enabling the feature.

alter table public.profiles
    add column if not exists daily_expense_notifications_enabled boolean not null default false,
    add column if not exists daily_expense_timezone text not null default 'Asia/Kolkata';

create table if not exists public.daily_expense_push_subscriptions (
    id bigserial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    endpoint text not null unique,
    p256dh text not null,
    auth text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists daily_expense_push_subscriptions_user_idx
    on public.daily_expense_push_subscriptions(user_id);

alter table public.daily_expense_push_subscriptions enable row level security;
drop policy if exists "Users manage their own daily expense push subscriptions"
    on public.daily_expense_push_subscriptions;
create policy "Users manage their own daily expense push subscriptions"
    on public.daily_expense_push_subscriptions
    for all to authenticated
    using (user_id = auth.uid())
    with check (user_id = auth.uid());
grant select, insert, update, delete
    on public.daily_expense_push_subscriptions to authenticated;
grant usage, select
    on sequence public.daily_expense_push_subscriptions_id_seq to authenticated;

-- The service-role cron worker claims a user/date before sending. A unique
-- key prevents duplicate notifications when Railway retries or overlaps jobs.
create table if not exists public.daily_expense_notification_sends (
    id bigserial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    summary_date date not null,
    created_at timestamptz not null default now(),
    unique (user_id, summary_date)
);
alter table public.daily_expense_notification_sends enable row level security;
revoke all on public.daily_expense_notification_sends from anon, authenticated;
