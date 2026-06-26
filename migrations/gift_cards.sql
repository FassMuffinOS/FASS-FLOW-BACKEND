-- FASS Gift Cards — prepaid dollar-balance Apple Wallet storeCard passes,
-- redeemable in-store by scanning the card's QR code with a staff phone
-- (same camera-scan pattern as Wallet Messaging's campaign redemption — see
-- wallet_campaigns.py / RedeemScan.jsx). No NFC/Apple VAS entitlement
-- required for this version: Apple gates true NFC-tap pass redemption
-- behind a separate, selective application (2-4+ week approval, certified
-- reader hardware), so this ships a QR-redeemable balance now and can be
-- upgraded to NFC later without changing the underlying data model — the
-- balance and redemption ledger stay exactly the same either way.

create table if not exists public.gift_cards (
  id                uuid primary key default uuid_generate_v4(),
  slug              text not null unique,
  business_user_id  uuid not null references auth.users(id) on delete cascade,
  customer_name     text,
  customer_contact  text,
  original_value    numeric not null,
  balance           numeric not null,
  active            boolean not null default true,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index if not exists idx_gift_cards_business on public.gift_cards(business_user_id);
create index if not exists idx_gift_cards_slug on public.gift_cards(slug);

create table if not exists public.gift_card_redemptions (
  id                uuid primary key default uuid_generate_v4(),
  gift_card_slug    text not null,
  business_user_id  uuid not null,
  amount            numeric not null,
  balance_after      numeric not null,
  redeemed_at       timestamptz not null default now()
);

create index if not exists idx_gift_card_redemptions_slug on public.gift_card_redemptions(gift_card_slug);
create index if not exists idx_gift_card_redemptions_business on public.gift_card_redemptions(business_user_id);

alter table public.gift_cards enable row level security;
alter table public.gift_card_redemptions enable row level security;

drop policy if exists "Owners manage their own gift cards" on public.gift_cards;
create policy "Owners manage their own gift cards" on public.gift_cards
  for all using (auth.uid() = business_user_id);

drop policy if exists "Owners manage their own gift card redemptions" on public.gift_card_redemptions;
create policy "Owners manage their own gift card redemptions" on public.gift_card_redemptions
  for all using (auth.uid() = business_user_id);
