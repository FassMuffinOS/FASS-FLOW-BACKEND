-- FASS Wallet live push — Apple PassKit Web Service registrations, plus the
-- updated_at/trigger that wallet_passes was missing (reward_cards and
-- reward_programs already had it). Run this in the Supabase SQL editor.

-- 1. wallet_passes needs the same updated_at pattern reward_cards/
--    reward_programs already use, so Apple's passesUpdatedSince tag
--    mechanism has something to compare against for business-identity
--    cards too. set_updated_at() itself already exists (created in
--    rewards_cards.sql) — reused here, not redefined.
alter table public.wallet_passes add column if not exists updated_at timestamptz not null default now();

drop trigger if exists trg_wallet_passes_updated_at on public.wallet_passes;
create trigger trg_wallet_passes_updated_at
  before update on public.wallet_passes
  for each row execute function set_updated_at();

-- 2. Device registrations — Apple Wallet itself calls the PassKit Web
--    Service endpoints (app/routers/wallet_passkit.py) whenever a customer
--    adds or removes a pass, so the backend knows which physical devices to
--    silently ping when that pass's data changes. One row per
--    (device, pass type, serial) triple — a single device can hold many
--    passes, and in theory more than one device could register interest in
--    the same pass (e.g. a customer's iPhone and Apple Watch both have it).
create table if not exists public.wallet_push_registrations (
  id                        uuid primary key default uuid_generate_v4(),
  device_library_identifier text not null,
  pass_type_identifier      text not null,
  serial_number             text not null,
  push_token                text not null,
  created_at                timestamptz not null default now(),
  unique (device_library_identifier, pass_type_identifier, serial_number)
);

create index if not exists idx_wallet_push_serial on public.wallet_push_registrations(serial_number);
create index if not exists idx_wallet_push_device on public.wallet_push_registrations(device_library_identifier, pass_type_identifier);

alter table public.wallet_push_registrations enable row level security;
-- The backend's service-role key bypasses RLS automatically; this table is
-- never queried with the anon/user key from the frontend, so there's no
-- end-user policy to add — RLS is enabled anyway purely as a safety net so
-- push tokens can never leak through an anon-key query by accident.
