-- Persistent in-app notification inbox for admin-sent notifications.
-- Notifications remain visible until the recipient explicitly clears them.

create table if not exists public.user_notifications (
    id bigserial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    title text not null check (char_length(title) between 2 and 80),
    message text not null check (char_length(message) between 2 and 240),
    created_at timestamptz not null default now(),
    cleared_at timestamptz
);

create index if not exists user_notifications_recipient_created_idx
    on public.user_notifications(user_id, created_at desc)
    where cleared_at is null;

alter table public.user_notifications enable row level security;

-- All reads and writes go through authenticated Flask routes using the
-- server-only service-role client, which scopes every operation to session user_id.
revoke all on public.user_notifications from anon, authenticated;
