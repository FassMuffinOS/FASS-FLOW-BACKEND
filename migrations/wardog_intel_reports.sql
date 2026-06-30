-- WARDOG Intel à la carte — $39 single-report purchase for non-Enterprise
-- users. Enterprise plan keeps unlimited access (see intelligence.py's
-- INTEL_PLANS check, unchanged); this table is the inventory of paid
-- one-off reports for everyone else. One row = one $39 purchase = one
-- incumbent/award-history pull + one AI forecast, scoped to a single
-- naics/agency pair. Run in the Supabase SQL editor.

create extension if not exists pgcrypto;

create table if not exists wardog_intel_reports (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references auth.users(id) on delete cascade,
  status        text not null default 'pending_payment', -- pending_payment | unused | opened | completed
  naics         text,
  agency        text,
  external_ref  text,  -- Stripe checkout session id, dedupes webhook redelivery
  created_at    timestamptz not null default now(),
  opened_at     timestamptz,
  completed_at  timestamptz
);

create index if not exists idx_wardog_intel_reports_user on wardog_intel_reports(user_id, created_at desc);

create unique index if not exists wardog_intel_reports_external_ref_uniq
  on public.wardog_intel_reports (external_ref)
  where external_ref is not null;

alter table wardog_intel_reports enable row level security;
drop policy if exists "Owners read their own intel reports" on wardog_intel_reports;
create policy "Owners read their own intel reports" on wardog_intel_reports
  for all using (auth.uid() = user_id);
