-- Track which PDF type was generated so history can accurately preview or download it.
alter table public.report_history
  add column if not exists report_type text not null default 'expense';

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'report_history_report_type_check'
      and conrelid = 'public.report_history'::regclass
  ) then
    alter table public.report_history
      add constraint report_history_report_type_check
      check (report_type in ('expense', 'cashflow', 'funfacts'));
  end if;
end $$;
