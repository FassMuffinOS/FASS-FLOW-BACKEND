-- BD Partner ($500/mo white-glove service) currently exists as pure
-- marketing copy with zero backend state — payment happens out-of-band via
-- a raw Stripe Payment Link (see BDPartner.jsx), so there's no webhook to
-- hang client status off of. This adds the minimum real state needed for
-- an active client to see an actual tool/log instead of the same sales
-- pitch they already paid past: who's an active client, and a timeline of
-- the work actually done for them (alerts surfaced, bids reviewed,
-- proposals drafted, calls, milestones) — populated by hand via the admin
-- endpoints in bd_partner.py, the same shared-secret pattern admin.py
-- already uses for manual onboarding.

create table if not exists public.bd_partner_clients (
  user_id uuid primary key references auth.users(id) on delete cascade,
  status text not null default 'active' check (status in ('active', 'paused', 'ended')),
  monthly_fee numeric not null default 500,
  plan_note text,
  started_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.bd_partner_activity (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  type text not null check (type in ('alert', 'review', 'draft', 'call', 'note', 'milestone')),
  title text not null,
  detail text,
  created_at timestamptz not null default now()
);

create index if not exists bd_partner_activity_user_idx on public.bd_partner_activity (user_id, created_at desc);

alter table public.bd_partner_clients enable row level security;
alter table public.bd_partner_activity enable row level security;

create policy "bd_partner_clients_owner_select" on public.bd_partner_clients
  for select using (auth.uid() = user_id);

create policy "bd_partner_activity_owner_select" on public.bd_partner_activity
  for select using (auth.uid() = user_id);

-- All writes (marking a client active, logging activity) go through the
-- service-role client behind the admin-secret-gated endpoints — these
-- policies only cover read access for any future direct client-side query.
