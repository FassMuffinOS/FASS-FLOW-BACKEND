-- Affiliate program: content creators sign up, get a referral code/link,
-- earn 30% commission on revenue FASS Flow actually collects from anyone
-- who signs up through their link (subscriptions, the Wallet pass unlock).
-- NOTE: gift card / campaign money is a destination charge straight to the
-- referred business's own Stripe Connect account (see gift_cards.py) — FASS
-- takes $0 of that today, so there is nothing real to commission there yet.
-- Masterclass and BD Partner are still raw out-of-band Stripe Payment
-- Links with no webhook, so those conversions are logged by hand in the
-- same admin console (mirrors bd_partner_activity's manual-logging pattern).

create table if not exists public.affiliates (
  user_id uuid primary key references auth.users(id) on delete cascade,
  code text not null unique,
  status text not null default 'active' check (status in ('active', 'paused')),
  commission_rate numeric not null default 0.30,
  created_at timestamptz not null default now()
);

create index if not exists affiliates_code_idx on public.affiliates (code);

create table if not exists public.affiliate_clicks (
  id uuid primary key default gen_random_uuid(),
  code text not null,
  landing_path text,
  created_at timestamptz not null default now()
);

create index if not exists affiliate_clicks_code_idx on public.affiliate_clicks (code, created_at desc);

create table if not exists public.affiliate_conversions (
  id uuid primary key default gen_random_uuid(),
  affiliate_user_id uuid not null references auth.users(id) on delete cascade,
  referred_user_id uuid references auth.users(id) on delete set null,
  source text not null check (source in ('subscription', 'wallet_pass', 'masterclass', 'bd_partner', 'other')),
  amount numeric not null,
  commission_amount numeric not null,
  status text not null default 'unpaid' check (status in ('unpaid', 'paid')),
  note text,
  created_at timestamptz not null default now()
);

create index if not exists affiliate_conversions_affiliate_idx on public.affiliate_conversions (affiliate_user_id, created_at desc);

create table if not exists public.affiliate_payouts (
  id uuid primary key default gen_random_uuid(),
  affiliate_user_id uuid not null references auth.users(id) on delete cascade,
  amount numeric not null,
  note text,
  paid_at timestamptz not null default now()
);

create index if not exists affiliate_payouts_affiliate_idx on public.affiliate_payouts (affiliate_user_id, paid_at desc);

-- Tracks who referred a given user, so a later payment event (or a manual
-- admin conversion entry) knows which affiliate to credit. Set once at
-- attribution time and never overwritten — first click wins.
alter table public.profiles add column if not exists referred_by_code text;

alter table public.affiliates enable row level security;
alter table public.affiliate_clicks enable row level security;
alter table public.affiliate_conversions enable row level security;
alter table public.affiliate_payouts enable row level security;

create policy "affiliates_owner_select" on public.affiliates
  for select using (auth.uid() = user_id);

create policy "affiliate_conversions_owner_select" on public.affiliate_conversions
  for select using (auth.uid() = affiliate_user_id);

create policy "affiliate_payouts_owner_select" on public.affiliate_payouts
  for select using (auth.uid() = affiliate_user_id);

-- affiliate_clicks has no owner-select policy — clicks are write-only from
-- the public landing page and only ever read back in aggregate by the
-- service-role client (GET /affiliates/me, admin list), never queried
-- directly by a client-side session.

-- All writes (joining, logging a click, recording a conversion, logging a
-- payout) go through the service-role client behind affiliates.py — these
-- policies only cover read access for any future direct client-side query.
