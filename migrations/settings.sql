-- Account Settings — one place for the toggles/preferences the new
-- /settings page (General, Notifications, Privacy & Security) reads and
-- writes. Mirrors ai_credits.sql's shape: owner-scoped row per user,
-- service-role app server enforces auth via auth_deps.require_owner, RLS
-- is the defense-in-depth backstop. Run in the Supabase SQL editor.

create extension if not exists pgcrypto;

create table if not exists user_preferences (
  user_id              uuid primary key references auth.users(id) on delete cascade,
  theme                text not null default 'dark',      -- 'dark' | 'light' — mirrors ThemeContext's localStorage value
  default_track        text not null default 'govcon',    -- 'govcon' | 'commercial' | 'startup' — see src/lib/track.js
  ai_auto_draft        boolean not null default false,     -- auto-run AI draft on new solicitations/proposals
  email_notifications  boolean not null default true,
  sms_notifications    boolean not null default true,
  push_notifications   boolean not null default true,
  updated_at           timestamptz not null default now()
);

create or replace function set_updated_at()
returns trigger as $$
begin new.updated_at = now(); return new; end;
$$ language plpgsql;

drop trigger if exists trg_user_preferences_updated_at on user_preferences;
create trigger trg_user_preferences_updated_at
  before update on user_preferences
  for each row execute function set_updated_at();

alter table user_preferences enable row level security;
drop policy if exists "Owners manage their own preferences" on user_preferences;
create policy "Owners manage their own preferences" on user_preferences
  for all using (auth.uid() = user_id);

-- Privacy & Security: data-export and account-deletion requests. These are
-- queued for manual/admin follow-up (beta honor-system, same trust model as
-- ai_credits' refill grants) rather than executed synchronously, since a
-- real export/delete touches a dozen tables and deserves a human check.
create table if not exists account_requests (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  type        text not null,             -- 'export' | 'delete'
  status      text not null default 'pending',  -- 'pending' | 'completed' | 'cancelled'
  notes       text,
  created_at  timestamptz not null default now()
);
create index if not exists idx_account_requests_user on account_requests(user_id, created_at desc);

alter table account_requests enable row level security;
drop policy if exists "Owners manage their own account requests" on account_requests;
create policy "Owners manage their own account requests" on account_requests
  for all using (auth.uid() = user_id);
