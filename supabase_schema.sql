-- =============================================
-- FASS Flow — Supabase Schema
-- Run in Supabase SQL editor
-- =============================================

-- Enable UUID extension
create extension if not exists "uuid-ossp";

-- ── Profiles ──────────────────────────────────
create table public.profiles (
  id                     uuid references auth.users(id) on delete cascade primary key,
  full_name              text,
  company_name           text,
  naics_codes            text[],
  sam_uei                text,
  stripe_customer_id     text unique,
  stripe_subscription_id text unique,
  plan                   text not null default 'free',
  subscription_status    text not null default 'trialing',
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

alter table public.profiles enable row level security;

create policy "Users can view own profile"
  on public.profiles for select using (auth.uid() = id);

create policy "Users can update own profile"
  on public.profiles for update using (auth.uid() = id);

-- Auto-create profile on signup
create or replace function public.handle_new_user()
returns trigger language plpgsql security definer as $$
begin
  insert into public.profiles (id, full_name)
  values (new.id, new.raw_user_meta_data->>'full_name');
  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ── Opportunities ──────────────────────────────
create table public.opportunities (
  id              uuid primary key default uuid_generate_v4(),
  sam_notice_id   text unique,
  title           text not null,
  agency          text,
  naics_code      text,
  set_aside       text,
  place_of_perf   text,
  posted_date     date,
  response_date   date,
  value_estimate  numeric,
  description     text,
  active          boolean default true,
  created_at      timestamptz not null default now()
);

alter table public.opportunities enable row level security;
create policy "Authenticated users can read opportunities"
  on public.opportunities for select using (auth.role() = 'authenticated');

-- ── Proposals ──────────────────────────────────
create table public.proposals (
  id              uuid primary key default uuid_generate_v4(),
  user_id         uuid references public.profiles(id) on delete cascade,
  opportunity_id  uuid references public.opportunities(id),
  title           text not null,
  status          text not null default 'draft',  -- draft|in_review|submitted|awarded|lost
  body            jsonb,
  submitted_at    timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

alter table public.proposals enable row level security;
create policy "Users can manage own proposals"
  on public.proposals for all using (auth.uid() = user_id);

-- ── Indexes ────────────────────────────────────
create index opp_naics_idx      on public.opportunities(naics_code);
create index opp_response_date  on public.opportunities(response_date);
create index prop_user_idx      on public.proposals(user_id);
create index prop_status_idx    on public.proposals(status);

-- ── Admin manual-grant tracking (run once for /admin/invite + /admin/grant-access) ──
alter table public.profiles add column if not exists admin_note text;
