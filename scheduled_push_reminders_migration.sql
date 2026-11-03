-- Consolidated push reminders: one claim per user, notification type and local target date.
create table if not exists public.scheduled_notification_sends (
    user_id uuid not null references auth.users(id) on delete cascade,
    notification_type text not null check (notification_type in ('upcoming_due', 'trip_start')),
    notification_date date not null,
    sent_at timestamptz not null default now(),
    primary key (user_id, notification_type, notification_date)
);

alter table public.scheduled_notification_sends enable row level security;
revoke all on table public.scheduled_notification_sends from anon, authenticated;
grant all on table public.scheduled_notification_sends to service_role;
