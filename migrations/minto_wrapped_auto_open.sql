-- One-time automatic opening markers for monthly and yearly Wrapped.
alter table public.profiles
  add column if not exists monthly_wrapped_seen text;
alter table public.profiles
  add column if not exists yearly_wrapped_seen integer;
