-- FASS Growth Challenge — "Build Your Business in 30 Days."
--
-- A separate gamification ledger from classroom_rewards (Masterclass nights)
-- and affiliates' XP/rank system (referral growth). Same one-row-per-user +
-- append-only-events shape those two already use, but its own XP step and
-- its own level ladder (Dreamer -> ... -> FASS Certified) because the three
-- features measure different things and don't share a leveling curve.
--
-- Missions are defined in code (src/data/growthChallengeMissions.js, mirrors
-- masterclassNights.js) rather than a table — the 30-day sequence is fixed
-- content, not user data. What IS user data, and lives here:
--   1. growth_challenge_state — one row per user: total XP, current day
--      reached, last activity (for streaks).
--   2. growth_challenge_completions — append-only log of which mission keys
--      a user has completed and when, so a mission can never be "uncompleted"
--      by a re-check and progress survives a frontend refresh.
--   3. growth_achievements — append-only log of real-business-milestone
--      badges (first proposal, first contract won, first $10k, ...),
--      deliberately separate from mission completions since achievements
--      are detected from real account state (business_events, proposals,
--      wallet_customers, etc.), not from a checkbox.
--
-- Run this in the Supabase SQL editor.

create table if not exists growth_challenge_state (
  user_id uuid primary key references auth.users(id) on delete cascade,
  xp integer not null default 0,
  current_day integer not null default 1,
  last_activity_date date,
  updated_at timestamptz not null default now()
);

create table if not exists growth_challenge_completions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  mission_key text not null,   -- e.g. "day-1-business-hq", matches growthChallengeMissions.js
  xp_awarded integer not null default 0,
  completed_at timestamptz not null default now(),
  unique (user_id, mission_key)
);

create index if not exists idx_growth_completions_user on growth_challenge_completions(user_id, completed_at desc);

create table if not exists growth_achievements (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  achievement_key text not null,  -- e.g. "first_proposal", "first_contract_won", "first_10k"
  label text not null,            -- human-readable, for the feed post / achievement grid
  xp_awarded integer not null default 0,
  earned_at timestamptz not null default now(),
  unique (user_id, achievement_key)
);

create index if not exists idx_growth_achievements_user on growth_achievements(user_id, earned_at desc);

drop trigger if exists trg_growth_challenge_state_updated_at on growth_challenge_state;
create trigger trg_growth_challenge_state_updated_at
  before update on growth_challenge_state
  for each row execute function set_updated_at();  -- reuses the function classroom_notebook.sql already created

alter table growth_challenge_state enable row level security;
alter table growth_challenge_completions enable row level security;
alter table growth_achievements enable row level security;

drop policy if exists "Owners manage their own growth challenge state" on growth_challenge_state;
create policy "Owners manage their own growth challenge state" on growth_challenge_state
  for all using (auth.uid() = user_id);

drop policy if exists "Owners manage their own growth challenge completions" on growth_challenge_completions;
create policy "Owners manage their own growth challenge completions" on growth_challenge_completions
  for all using (auth.uid() = user_id);

drop policy if exists "Owners manage their own growth achievements" on growth_achievements;
create policy "Owners manage their own growth achievements" on growth_achievements
  for all using (auth.uid() = user_id);
