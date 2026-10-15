-- MIN-9: private plain-text chat scoped to current trip membership.
begin;
create table if not exists public.trip_chat_messages (
    id bigserial primary key,
    trip_id integer not null references public.trips(id) on delete cascade,
    sender_user_id uuid not null references auth.users(id) on delete cascade,
    body text not null check (char_length(btrim(body)) between 1 and 4000),
    created_at timestamptz not null default now()
);
create index if not exists trip_chat_messages_trip_created_idx
    on public.trip_chat_messages (trip_id, created_at desc, id desc);
alter table public.trip_chat_messages enable row level security;

drop policy if exists "trip chat members can read messages" on public.trip_chat_messages;
create policy "trip chat members can read messages"
    on public.trip_chat_messages for select to authenticated
    using (
        exists (select 1 from public.trips t
                where t.id = trip_chat_messages.trip_id and t.user_id = auth.uid())
        or exists (select 1 from public.trip_members tm
                   where tm.trip_id = trip_chat_messages.trip_id
                     and tm.user_id = auth.uid() and tm.active = true)
    );

drop policy if exists "trip chat members can send messages" on public.trip_chat_messages;
create policy "trip chat members can send messages"
    on public.trip_chat_messages for insert to authenticated
    with check (
        sender_user_id = auth.uid()
        and (
            exists (select 1 from public.trips t
                    where t.id = trip_chat_messages.trip_id and t.user_id = auth.uid())
            or exists (select 1 from public.trip_members tm
                       where tm.trip_id = trip_chat_messages.trip_id
                         and tm.user_id = auth.uid() and tm.active = true)
        )
    );
grant select, insert on public.trip_chat_messages to authenticated;
grant usage, select on sequence public.trip_chat_messages_id_seq to authenticated;
commit;
