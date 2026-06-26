-- Unified business profile — the shared "Customer 360" record across
-- Start Business, Wallet, and Rewards. Each tool owns a slice of the row
-- (Wallet writes identity fields it already looks up via Google Places;
-- Start Business writes structure/path/checklist progress) so information
-- entered once in any tool shows up in the others instead of living in
-- per-tool localStorage silos that never talk to each other.
-- Run this in the Supabase SQL editor.

create table if not exists business_profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  business_name text,
  address text,
  naics text,
  website text,
  phone text,
  structure text,   -- 'sole_prop' | 'llc' | 'other' — set from Start Business
  biz_path text,    -- 'product' | 'service' — set from Start Business
  checklist jsonb not null default '{}'::jsonb,  -- { stepId: true, ... }
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Reuses the same trigger function rewards_cards.sql already created; safe
-- to run even if that migration already defined it.
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_business_profiles_updated_at on business_profiles;
create trigger trg_business_profiles_updated_at
  before update on business_profiles
  for each row execute function set_updated_at();

alter table business_profiles enable row level security;

drop policy if exists "Owners manage their own business profile" on business_profiles;
create policy "Owners manage their own business profile" on business_profiles
  for all using (auth.uid() = user_id);
