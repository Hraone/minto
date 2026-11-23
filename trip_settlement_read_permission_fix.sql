-- Fix Trip Mode settlement history read permission.
-- RLS policies, if enabled, continue to control which rows authenticated users can see.
grant select on table public.trip_settlements to authenticated;

-- The delete action itself is exposed only through the SECURITY DEFINER RPC.
notify pgrst, 'reload schema';
