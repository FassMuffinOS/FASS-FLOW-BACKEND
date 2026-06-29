-- Reuse Engine — the company "gold standard" content library.
-- The biggest time sink in govcon proposal work is rewriting the same
-- Quality Control / Management Approach / Past Performance narratives on
-- every bid. This table stores those once, per company, and the Proposal
-- Editor surfaces them back as one-click inserts matched to the section
-- being written (see app/routers/reuse_library.py).
--
-- Owner-scoped private data, same model as business_profiles: one row per
-- block, owned by a single user. Run this in the Supabase SQL editor.

create extension if not exists pgcrypto;  -- for gen_random_uuid()

create table if not exists reuse_blocks (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users(id) on delete cascade,
  title      text not null,
  category   text,            -- technical_approach | management | past_performance
                              -- | quality_control | staffing | price | safety
                              -- | transition | other
  body       text not null,
  source     text not null default 'manual',  -- 'manual' | 'captured'
  use_count  int  not null default 0,         -- bumped on insert-into-proposal,
                                              -- so proven content ranks first
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Suggestions filter by (user, category); listing orders by use_count.
create index if not exists idx_reuse_blocks_user_cat on reuse_blocks(user_id, category);

-- Reuses the shared trigger function earlier migrations already define;
-- create-or-replace makes this safe to run standalone too.
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_reuse_blocks_updated_at on reuse_blocks;
create trigger trg_reuse_blocks_updated_at
  before update on reuse_blocks
  for each row execute function set_updated_at();

-- The API runs on the service-role key and enforces ownership in the router
-- (require_owner), but RLS is enabled anyway as defense-in-depth, matching
-- business_profiles.sql.
alter table reuse_blocks enable row level security;

drop policy if exists "Owners manage their own reuse blocks" on reuse_blocks;
create policy "Owners manage their own reuse blocks" on reuse_blocks
  for all using (auth.uid() = user_id);
