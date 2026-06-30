-- Regulars — standalone Wallet/loyalty product for non-govcon local
-- businesses (Apple Wallet passes, gift cards, rewards punch cards,
-- campaign broadcasts, SMS), spun out from the GovCon platform's existing
-- Wallet system. Mirrors the is_affiliate_only pattern in
-- affiliates_partner_program.sql: a real auth.users + profiles row, just
-- flagged so the app shell renders the stripped Regulars-only chrome
-- instead of the full GovCon product. Run in the Supabase SQL editor.

alter table public.profiles
  add column if not exists is_wallet_only boolean not null default false;

alter table public.profiles
  add column if not exists wallet_plan text;  -- 'starter' | 'pro', null until first payment

alter table public.profiles
  add column if not exists wallet_billing_interval text;  -- 'monthly' | 'annual'

-- Deliberately SEPARATE from stripe_customer_id/stripe_subscription_id
-- (the GovCon subscription columns) rather than reused, even though today
-- every Regulars signup provisions a brand new auth.users row with no
-- GovCon subscription to collide with. A business that's both a GovCon
-- customer AND a Regulars customer on the same login is plausible down the
-- road, and sharing columns would mean the second purchase silently
-- overwrites the first one's Stripe references.
alter table public.profiles
  add column if not exists wallet_stripe_customer_id text;

alter table public.profiles
  add column if not exists wallet_stripe_subscription_id text;

alter table public.profiles
  add column if not exists wallet_subscription_status text;  -- 'active' | 'past_due' | 'cancelled'
