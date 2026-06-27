-- Multi-entity support for Start Business. business_profiles stays exactly
-- as it was (one row per user_id) and keeps being the table Wallet/Rewards/
-- Start Business read via /business-profile/mine — it's now treated as a
-- mirror of whichever entity below is "active," so none of those tools
-- needed to change. business_entities is the real multi-business store:
-- Free/Core = 1 entity, Pro = 3, Team = unlimited (enforced in
-- app/routers/business_profile.py, not here).
-- Run this in the Supabase SQL editor.

create table if not exists business_entities (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  business_name text,
  address text,
  naics text,
  website text,
  phone text,
  structure text,   -- 'sole_prop' | 'llc' | 'other'
  biz_path text,    -- 'product' | 'service'
  checklist jsonb not null default '{}'::jsonb,
  is_primary boolean not null default false, -- the original entity on the account, can't be deleted while it's the only one
  active boolean not null default false,     -- which entity business_profiles currently mirrors
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists business_entities_user_idx on business_entities(user_id);

-- Reuses the trigger function business_profiles.sql already created.
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_business_entities_updated_at on business_entities;
create trigger trg_business_entities_updated_at
  before update on business_entities
  for each row execute function set_updated_at();

alter table business_entities enable row level security;

drop policy if exists "Owners manage their own business entities" on business_entities;
create policy "Owners manage their own business entities" on business_entities
  for all using (auth.uid() = user_id);

-- One-time backfill: every existing business_profiles row becomes that
-- user's first (primary, active) entity. Safe to re-run — skips users who
-- already have an entity row.
insert into business_entities (user_id, business_name, address, naics, website, phone, structure, biz_path, checklist, is_primary, active, created_at, updated_at)
select bp.user_id, bp.business_name, bp.address, bp.naics, bp.website, bp.phone, bp.structure, bp.biz_path, bp.checklist, true, true, bp.created_at, bp.updated_at
from business_profiles bp
where not exists (select 1 from business_entities be where be.user_id = bp.user_id);
