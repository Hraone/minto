-- Trip Chat push notifications and member display-name support.
begin;

alter table public.profiles
  add column if not exists trip_chat_notifications_enabled boolean not null default true;

create table if not exists public.trip_chat_push_subscriptions (
  id bigserial primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  endpoint text not null unique,
  p256dh text not null,
  auth text not null,
  updated_at timestamptz not null default now()
);

create index if not exists trip_chat_push_subscriptions_user_idx
  on public.trip_chat_push_subscriptions(user_id);

alter table public.trip_chat_push_subscriptions enable row level security;

drop policy if exists "users manage own trip chat push subscriptions"
  on public.trip_chat_push_subscriptions;

create policy "users manage own trip chat push subscriptions"
  on public.trip_chat_push_subscriptions
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

grant select, insert, update, delete
  on public.trip_chat_push_subscriptions to authenticated;
grant usage, select
  on sequence public.trip_chat_push_subscriptions_id_seq to authenticated;

commit;
