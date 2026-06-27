-- Business Health event log — Phase 1 of the "Gmailification of Business" build.
--
-- Every module (Classroom, Pipeline, FASS FILL, WITNESS, Wallet Campaigns,
-- Gift Cards, ...) writes a row here whenever the user does something that
-- should move the needle on their business. This single table is the shared
-- backbone for three later features that all read from it instead of each
-- needing their own data model:
--   1. The Business Health score (sum of points per category, capped, shown
--      on the Dashboard as a 0-100 ring + daily delta).
--   2. The Inbox/activity feed (Phase 2) — "what happened since I left."
--   3. The Business Timeline (Phase 3) — a permanent, append-only history.
--
-- Run this in the Supabase SQL editor.

create table if not exists business_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  category text not null check (category in (
    'government_readiness', 'customer_growth', 'operations', 'documentation', 'marketing'
  )),
  action text not null,        -- short machine label, e.g. "mission_complete", "stage_change_awarded"
  label text,                  -- human-readable, e.g. "Completed Mission 3" — what the Inbox/Timeline will show
  points integer not null default 0,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_business_events_user_created on business_events(user_id, created_at desc);
create index if not exists idx_business_events_user_category on business_events(user_id, category);

alter table business_events enable row level security;

drop policy if exists "Owners manage their own business events" on business_events;
create policy "Owners manage their own business events" on business_events
  for all using (auth.uid() = user_id);
