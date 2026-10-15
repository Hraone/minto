-- MIN-9 follow-up: per-user trip chat read markers / unread counts.
-- Apply after trip_chat_migration.sql. Does not alter or disable existing RLS.
begin;

create table if not exists public.trip_chat_reads (
    trip_id integer not null references public.trips(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    last_read_at timestamptz not null default now(),
    primary key (trip_id, user_id)
);

alter table public.trip_chat_reads enable row level security;

drop policy if exists "trip members manage own chat read marker" on public.trip_chat_reads;
create policy "trip members manage own chat read marker"
    on public.trip_chat_reads for all to authenticated
    using (
        user_id = auth.uid()
        and (
            exists (select 1 from public.trips t
                    where t.id = trip_chat_reads.trip_id and t.user_id = auth.uid())
            or exists (select 1 from public.trip_members tm
                       where tm.trip_id = trip_chat_reads.trip_id
                         and tm.user_id = auth.uid() and tm.active = true)
        )
    )
    with check (
        user_id = auth.uid()
        and (
            exists (select 1 from public.trips t
                    where t.id = trip_chat_reads.trip_id and t.user_id = auth.uid())
            or exists (select 1 from public.trip_members tm
                       where tm.trip_id = trip_chat_reads.trip_id
                         and tm.user_id = auth.uid() and tm.active = true)
        )
    );

grant select, insert, update on public.trip_chat_reads to authenticated;
commit;
