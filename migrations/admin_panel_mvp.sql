-- Minto Admin Panel MVP migration. Review and apply manually in Supabase.
-- The first administrator is provisioned explicitly at the bottom; no user is
-- auto-promoted by this migration.

begin;

create table if not exists public.admin_roles (
    user_id uuid primary key references auth.users(id) on delete restrict,
    created_at timestamptz not null default now()
);
alter table public.admin_roles enable row level security;
revoke all on table public.admin_roles from anon, authenticated;

create table if not exists public.admin_activity_logs (
    id bigserial primary key,
    actor_user_id uuid not null references auth.users(id) on delete restrict,
    target_type text not null check (target_type in ('user', 'feedback')),
    target_id text not null,
    action text not null,
    outcome text not null check (outcome in ('success', 'denied', 'not_found', 'failed')),
    details jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);
create index if not exists admin_activity_logs_created_idx on public.admin_activity_logs (created_at desc);
create index if not exists admin_activity_logs_actor_created_idx on public.admin_activity_logs (actor_user_id, created_at desc);
alter table public.admin_activity_logs enable row level security;
revoke all on table public.admin_activity_logs from anon, authenticated;

alter table public.user_feedback add column if not exists priority text not null default 'Normal'
    check (priority in ('Low', 'Normal', 'High', 'Urgent'));
create index if not exists user_feedback_status_priority_created_idx
    on public.user_feedback (status, priority, created_at desc);

create or replace function public.admin_set_role(p_target_user_id uuid, p_grant_access boolean, p_actor_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, auth
as $$
declare
    v_changed integer := 0;
    v_admin_count integer := 0;
    v_target_is_admin boolean := false;
    v_action text := case when p_grant_access then 'grant_admin' else 'revoke_admin' end;
begin
    perform pg_advisory_xact_lock(hashtext('minto_admin_role_mutation'));
    if not exists (select 1 from public.admin_roles where user_id = p_actor_user_id) then
        insert into public.admin_activity_logs (actor_user_id,target_type,target_id,action,outcome,details)
        values (p_actor_user_id,'user',coalesce(p_target_user_id::text,''),v_action,'denied',jsonb_build_object('reason','actor_not_admin'));
        return jsonb_build_object('ok',false,'reason','forbidden');
    end if;
    if p_target_user_id is null or not exists (select 1 from auth.users where id = p_target_user_id) then
        insert into public.admin_activity_logs (actor_user_id,target_type,target_id,action,outcome,details)
        values (p_actor_user_id,'user',coalesce(p_target_user_id::text,''),v_action,'not_found','{}'::jsonb);
        return jsonb_build_object('ok',false,'reason','not_found');
    end if;
    if p_grant_access and p_target_user_id = p_actor_user_id then
        insert into public.admin_activity_logs (actor_user_id,target_type,target_id,action,outcome,details)
        values (p_actor_user_id,'user',p_target_user_id::text,v_action,'denied',jsonb_build_object('reason','self_grant'));
        return jsonb_build_object('ok',false,'reason','self_grant');
    end if;
    if not p_grant_access then
        select exists(select 1 from public.admin_roles where user_id = p_target_user_id) into v_target_is_admin;
        if v_target_is_admin then
            select count(*) into v_admin_count from public.admin_roles;
            if v_admin_count <= 1 then
                insert into public.admin_activity_logs (actor_user_id,target_type,target_id,action,outcome,details)
                values (p_actor_user_id,'user',p_target_user_id::text,v_action,'denied',jsonb_build_object('reason','last_admin'));
                return jsonb_build_object('ok',false,'reason','last_admin');
            end if;
        end if;
        delete from public.admin_roles where user_id = p_target_user_id;
        get diagnostics v_changed = row_count;
    else
        insert into public.admin_roles (user_id) values (p_target_user_id) on conflict (user_id) do nothing;
        get diagnostics v_changed = row_count;
    end if;
    insert into public.admin_activity_logs (actor_user_id,target_type,target_id,action,outcome,details)
    values (p_actor_user_id,'user',p_target_user_id::text,v_action,'success',jsonb_build_object('changed',v_changed > 0));
    return jsonb_build_object('ok',true,'changed',v_changed > 0,'action',v_action);
end;
$$;

create or replace function public.admin_update_feedback(p_feedback_id bigint, p_status text, p_priority text, p_actor_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, auth
as $$
declare
    v_feedback_user_id uuid;
begin
    if not exists (select 1 from public.admin_roles where user_id = p_actor_user_id) then
        insert into public.admin_activity_logs (actor_user_id,target_type,target_id,action,outcome,details)
        values (p_actor_user_id,'feedback',p_feedback_id::text,'update_feedback_workflow','denied',jsonb_build_object('reason','actor_not_admin'));
        return jsonb_build_object('ok',false,'reason','forbidden');
    end if;
    if p_status not in ('Submitted','Under Review','In Progress','Resolved','Closed','Declined')
       or p_priority not in ('Low','Normal','High','Urgent') then
        raise exception 'Unsupported feedback workflow value';
    end if;
    update public.user_feedback set status=p_status, priority=p_priority, updated_at=now()
    where id=p_feedback_id returning user_id into v_feedback_user_id;
    if not found then
        insert into public.admin_activity_logs (actor_user_id,target_type,target_id,action,outcome,details)
        values (p_actor_user_id,'feedback',p_feedback_id::text,'update_feedback_workflow','not_found','{}'::jsonb);
        return jsonb_build_object('ok',false,'reason','not_found');
    end if;
    insert into public.admin_activity_logs (actor_user_id,target_type,target_id,action,outcome,details)
    values (p_actor_user_id,'feedback',p_feedback_id::text,'update_feedback_workflow','success',jsonb_build_object('status',p_status,'priority',p_priority));
    return jsonb_build_object('ok',true,'feedback_id',p_feedback_id);
end;
$$;

revoke all on function public.admin_set_role(uuid, boolean, uuid) from public, anon, authenticated;
revoke all on function public.admin_update_feedback(bigint, text, text, uuid) from public, anon, authenticated;
grant execute on function public.admin_set_role(uuid, boolean, uuid) to service_role;
grant execute on function public.admin_update_feedback(bigint, text, text, uuid) to service_role;

commit;

-- After reviewing the target account, replace the placeholder and run this
-- statement separately to bootstrap the first administrator:
-- insert into public.admin_roles (user_id)
-- select id from auth.users where lower(email)=lower('REPLACE_WITH_TRUSTED_ADMIN_EMAIL')
-- on conflict (user_id) do nothing;
