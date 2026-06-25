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

-- ── Lite plan AI quota tracking (run once for the $9.99/mo Lite tier) ──
-- Lite is capped at 1 AI synthesis (read-synthesis, cost-breakdown, etc.)
-- per 30-day billing cycle; every other plan is unlimited. ai_quota_reset_at
-- starts null so the first AI call on a profile initializes the cycle.
alter table public.profiles add column if not exists ai_quota_used integer not null default 0;
alter table public.profiles add column if not exists ai_quota_reset_at timestamptz;

-- ── FASS Wallet (.pkpass) one-time purchases ──────────────────────────
-- Free preview renders client-side from business_lookup.py's result with
-- no row here at all. A row is only created once someone clicks "Unlock &
-- add to Apple Wallet" — it tracks the one-time Stripe payment for a real,
-- signed .pkpass tied to one business. slug is the public QR target
-- (flow.fass.systems/c/{slug}) and the lookup key wallet.py's /pass and
-- /purchase-status endpoints use; it never changes once created.
create table if not exists public.wallet_passes (
  id                 uuid primary key default uuid_generate_v4(),
  user_id            uuid references public.profiles(id) on delete cascade,
  slug               text unique not null,
  business_name      text not null,
  address            text,
  naics              text,
  website            text,
  phone              text,
  stripe_session_id  text,
  purchased          boolean not null default false,
  purchased_at       timestamptz,
  created_at         timestamptz not null default now()
);

alter table public.wallet_passes enable row level security;
create policy "Users can manage own wallet passes"
  on public.wallet_passes for all using (auth.uid() = user_id);

create index if not exists wallet_passes_slug_idx on public.wallet_passes(slug);

-- ── FASS Wallet card customization ────────────────────────────────────
-- Free to design in the preview, before any purchase exists — these ride
-- along on the same wallet_passes row created at checkout time, then
-- generate_pkpass() (applewallet.py) reads them back to brand the real,
-- signed pass. bg_color is a hex string ("#240e41"); logo_url points into
-- the public wallet-logos storage bucket below.
alter table public.wallet_passes add column if not exists bg_color text not null default '#240e41';
alter table public.wallet_passes add column if not exists logo_url text;
alter table public.wallet_passes add column if not exists show_address boolean not null default true;
alter table public.wallet_passes add column if not exists show_naics boolean not null default true;
alter table public.wallet_passes add column if not exists show_phone boolean not null default true;
alter table public.wallet_passes add column if not exists show_website boolean not null default true;

-- ── FASS Wallet logo uploads (Storage bucket) ─────────────────────────
-- Public read so logos can be embedded in a downloaded .pkpass and on the
-- public /c/{slug} capability page; insert restricted to authenticated
-- users uploading their own logo from Passport's customization step.
insert into storage.buckets (id, name, public)
values ('wallet-logos', 'wallet-logos', true)
on conflict (id) do nothing;

create policy "Public read wallet logos"
  on storage.objects for select
  using (bucket_id = 'wallet-logos');

create policy "Authenticated users can upload wallet logos"
  on storage.objects for insert
  to authenticated
  with check (bucket_id = 'wallet-logos');
