-- Classroom Notebook + rewards system.
--
-- Three pieces, one goal: the Masterclass should feel like a real college
-- class (gradebook, transcript, study notebook) and the work a student does
-- in it should become part of the system they actually run their business
-- on, not disappear into a textarea no other tool ever reads.
--
-- 1. classroom_rewards — one row per student. The "college class" gamified
--    layer: XP, level, a study streak, earned badges, and a stamp count
--    that mirrors the existing FASS Rewards stamp-card metaphor without
--    touching the real Apple Wallet pipeline (that pipeline is for
--    businesses rewarding their own customers — a different relationship).
--
-- 2. classroom_notebook — the actual "Google NotebookLM" surface. Every
--    chat turn with the AI assistant and every personalized insight the
--    assistant generates after a night's homework is saved here, grouped
--    by night, so a student can come back and re-read what the assistant
--    told them about THEIR business, not generic course content.
--
-- 3. business_profiles.notebook_keywords / notebook_summary — the "becomes
--    their system" hook. When the assistant extracts something durable
--    about the student's niche (keywords, a one-line positioning summary),
--    it's written here so WARDOG and the rest of the app can read it back
--    instead of the insight living only inside the Classroom.
--
-- Run this in the Supabase SQL editor.

create table if not exists classroom_rewards (
  user_id uuid primary key references auth.users(id) on delete cascade,
  xp integer not null default 0,
  level integer not null default 1,
  streak_count integer not null default 0,
  last_activity_date date,
  badges jsonb not null default '[]'::jsonb,
  stamps integer not null default 0,
  updated_at timestamptz not null default now()
);

create table if not exists classroom_notebook (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  night integer not null,
  entry_type text not null check (entry_type in ('chat_user', 'chat_assistant', 'insight')),
  content text not null,
  meta jsonb not null default '{}'::jsonb,  -- e.g. {"niche_keywords": [...], "provider": "anthropic"}
  created_at timestamptz not null default now()
);

create index if not exists idx_classroom_notebook_user on classroom_notebook(user_id, night, created_at);

alter table business_profiles
  add column if not exists notebook_keywords jsonb not null default '[]'::jsonb,
  add column if not exists notebook_summary text;

create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_classroom_rewards_updated_at on classroom_rewards;
create trigger trg_classroom_rewards_updated_at
  before update on classroom_rewards
  for each row execute function set_updated_at();

alter table classroom_rewards enable row level security;
alter table classroom_notebook enable row level security;

drop policy if exists "Owners manage their own classroom rewards" on classroom_rewards;
create policy "Owners manage their own classroom rewards" on classroom_rewards
  for all using (auth.uid() = user_id);

drop policy if exists "Owners manage their own notebook entries" on classroom_notebook;
create policy "Owners manage their own notebook entries" on classroom_notebook
  for all using (auth.uid() = user_id);
