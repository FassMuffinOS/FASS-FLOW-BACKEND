-- Gamification shell for the affiliate program (FASS Creator OS, phase 1):
-- XP, levels, rank titles, and an idempotent XP-event ledger so an action
-- (completing profile, joining, a daily assignment, a conversion, a
-- recruit) only ever awards XP once. Run AFTER affiliates.sql and
-- affiliates_recruiting.sql (additive only).

alter table public.affiliates add column if not exists xp integer not null default 0;

-- One row per XP-earning action. `action` is a unique-per-affiliate key —
-- for one-time actions it's a fixed string ("join", "complete_profile",
-- "watch_onboarding"); for repeatable actions it encodes the thing that
-- makes it unique (a date for daily assignments, a conversion id for
-- conversions, a recruit's user id for a new recruit) so the same event
-- can never be double-counted even if an endpoint is called twice.
create table if not exists public.affiliate_xp_events (
  id uuid primary key default gen_random_uuid(),
  affiliate_user_id uuid not null references auth.users(id) on delete cascade,
  action text not null,
  xp_amount integer not null,
  note text,
  created_at timestamptz not null default now(),
  unique (affiliate_user_id, action)
);

create index if not exists affiliate_xp_events_affiliate_idx on public.affiliate_xp_events (affiliate_user_id, created_at desc);

alter table public.affiliate_xp_events enable row level security;

create policy "affiliate_xp_events_owner_select" on public.affiliate_xp_events
  for select using (auth.uid() = affiliate_user_id);

-- All writes go through the service-role client behind affiliates.py.
