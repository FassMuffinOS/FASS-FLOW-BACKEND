-- FASS Wallet Messaging — one-way broadcast campaigns (offers/coupons) pushed
-- onto customers' existing FASS Rewards storeCard passes. Run in the
-- Supabase SQL editor.
--
-- Deliberately rides on reward_cards (the customer-facing pass every
-- business's customers already have in Wallet for stamps) rather than
-- inventing a second customer-facing pass type — a campaign just sets a
-- temporary "offer" field on cards a business already issued, then pushes
-- those devices so it shows up without a re-download (see
-- app/services/apns.py + app/routers/wallet_passkit.py, built earlier).

create table if not exists public.wallet_campaigns (
  id                uuid primary key default uuid_generate_v4(),
  business_user_id  uuid not null references auth.users(id) on delete cascade,
  message           text not null,        -- short line shown on the card + push banner, e.g. "Free lemonade with wing combo today"
  detail            text,                  -- optional longer text for the pass's back field
  expires_at        timestamptz,           -- null = no expiration
  repeat_use        boolean not null default false,  -- false = offer clears off a card the moment it's redeemed once
  active            boolean not null default true,
  sent_count        int not null default 0,          -- snapshot of how many cards this went out to, taken at send time
  estimated_value   numeric,               -- business-entered avg $ value per redemption, purely for the dashboard's revenue-estimate display (redeemed_count * estimated_value) — never charged/billed, just a planning number
  created_at        timestamptz not null default now()
);

create index if not exists idx_wallet_campaigns_business on public.wallet_campaigns(business_user_id);

create table if not exists public.wallet_campaign_redemptions (
  id                uuid primary key default uuid_generate_v4(),
  campaign_id       uuid not null references public.wallet_campaigns(id) on delete cascade,
  card_slug         text not null,
  business_user_id  uuid not null,
  redeemed_at       timestamptz not null default now()
);

create index if not exists idx_campaign_redemptions_campaign on public.wallet_campaign_redemptions(campaign_id);
create index if not exists idx_campaign_redemptions_slug on public.wallet_campaign_redemptions(card_slug);

-- Which campaign's offer (if any) is currently "live" on a given customer's
-- card. Null = no active offer showing. Cleared automatically on redemption
-- for one-time campaigns (repeat_use=false); left in place for repeat-use
-- campaigns until it expires or the business deactivates it.
alter table public.reward_cards add column if not exists active_campaign_id uuid references public.wallet_campaigns(id) on delete set null;

alter table public.wallet_campaigns enable row level security;
alter table public.wallet_campaign_redemptions enable row level security;

drop policy if exists "Owners manage their own campaigns" on public.wallet_campaigns;
create policy "Owners manage their own campaigns" on public.wallet_campaigns
  for all using (auth.uid() = business_user_id);

drop policy if exists "Owners manage their own campaign redemptions" on public.wallet_campaign_redemptions;
create policy "Owners manage their own campaign redemptions" on public.wallet_campaign_redemptions
  for all using (auth.uid() = business_user_id);
