-- Minto private user feedback MVP.
-- Apply in Supabase SQL Editor after reviewing the target environment.
-- This migration is additive and does not modify existing finance tables.

create table if not exists public.user_feedback (
    id bigserial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    category text not null
        check (category in (
            'General Feedback',
            'UI/UX Improvement',
            'Feature Request',
            'Bug Report',
            'Performance Issue',
            'Other'
        )),
    rating integer
        check (rating is null or rating between 1 and 5),
    message text not null
        check (length(btrim(message)) between 1 and 5000),
    status text not null default 'Submitted'
        check (status in (
            'Submitted',
            'Under Review',
            'In Progress',
            'Resolved',
            'Closed',
            'Declined'
        )),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists user_feedback_owner_created_idx
    on public.user_feedback (user_id, created_at desc);

alter table public.user_feedback enable row level security;

drop policy if exists "users read own feedback" on public.user_feedback;
create policy "users read own feedback"
    on public.user_feedback
    for select
    to authenticated
    using (auth.uid() = user_id);

drop policy if exists "users submit own feedback" on public.user_feedback;
create policy "users submit own feedback"
    on public.user_feedback
    for insert
    to authenticated
    with check (auth.uid() = user_id);

-- Private MVP only: users can read their own records and submit new records.
-- No UPDATE or DELETE privilege is granted to the authenticated client.
revoke all on table public.user_feedback from anon;
grant select (
    id, user_id, category, rating, message, status, created_at, updated_at
) on public.user_feedback to authenticated;
grant insert (user_id, category, rating, message)
    on public.user_feedback to authenticated;
grant usage, select on sequence public.user_feedback_id_seq to authenticated;

-- Rollback (only if the feature is being removed and its data may be deleted):
-- drop table if exists public.user_feedback;
