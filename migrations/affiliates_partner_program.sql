-- External Affiliate Application Program — lets someone become an
-- affiliate WITHOUT being a FASS Flow customer first. Builds on top of the
-- existing self-serve affiliate system (affiliates.sql + _recruiting +
-- _gamification) rather than replacing it: an external applicant still
-- gets a real auth.users row + a row in public.affiliates (so they inherit
-- gamification, recruiting, the dashboard, and the admin console for
-- free), but is flagged is_affiliate_only so the app shell can render a
-- stripped creator-only nav instead of the full GovCon product the
-- affiliates.user_id FK schema was built for. "Separate but linked
-- through" without restructuring the existing auth.users-coupled schema.
--
-- Run AFTER affiliates.sql, affiliates_recruiting.sql, affiliates_gamification.sql.

-- Applicant-only flag, kept on profiles (not affiliates) so it's visible
-- everywhere a profile already is fetched — AppShell's GET
-- /users/{id}/profile call needs zero changes to start seeing this.
alter table public.profiles
  add column if not exists is_affiliate_only boolean not null default false;

-- The moment referred_by_code actually gets set. The existing column had
-- no timestamp; 12-month recurring commission needs a fixed start point
-- per referred customer to know when the window closes.
alter table public.profiles
  add column if not exists referred_at timestamptz;

-- Backfill: anyone already referred before this migration starts a fresh
-- 12-month window from today rather than being silently cut off.
update public.profiles
  set referred_at = now()
  where referred_by_code is not null and referred_at is null;

-- The actual "who/why/how" pitch from an external applicant. One row per
-- application; kept separate from public.affiliates so that table's lean
-- shape (code/status/commission_rate/xp/...) is untouched.
create table if not exists public.affiliate_applications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  full_name text,
  email text not null,
  platform text,             -- instagram / youtube / tiktok / blog / newsletter / other
  channel_url text,
  audience_size text,        -- free-text band, e.g. "1k-10k" — informational only, not verified
  why_join text,
  how_promote text,
  status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  reviewed_at timestamptz,
  notes text,
  created_at timestamptz not null default now()
);

create index if not exists affiliate_applications_user_idx on public.affiliate_applications (user_id);
create index if not exists affiliate_applications_status_idx on public.affiliate_applications (status);

alter table public.affiliate_applications enable row level security;
drop policy if exists "applicants can view their own application" on public.affiliate_applications;
create policy "applicants can view their own application"
  on public.affiliate_applications for select
  using (auth.uid() = user_id);

-- Bounded commission window (months) instead of lifetime-recurring.
-- Measured from profiles.referred_at. Per-affiliate column (not a global
-- constant) so it can be tuned per program tier later without a migration.
alter table public.affiliates
  add column if not exists commission_window_months integer not null default 12;

-- Distinguishes the original "no application" self-serve joiners (still
-- fully supported, unaffected) from the new application-gated external
-- partner flow. Informational only — doesn't change what either group
-- can do today.
alter table public.affiliates
  add column if not exists source text not null default 'self_serve'
    check (source in ('self_serve', 'application'));

-- Dedupe key for recurring commission. Stripe's webhook delivery is
-- at-least-once, and invoice.payment_succeeded now drives commission for
-- BOTH the first and every renewal invoice (see subscriptions.py) — without
-- this, a redelivered event would double-pay. Nullable + a partial unique
-- index (not NOT NULL unique) so existing rows and the admin manual-entry
-- path (which has no Stripe invoice id) are unaffected.
alter table public.affiliate_conversions
  add column if not exists external_ref text;

create unique index if not exists affiliate_conversions_external_ref_uniq
  on public.affiliate_conversions (external_ref)
  where external_ref is not null;
