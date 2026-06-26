-- Stripe Connect — gives each business on FASS Flow its own linked Stripe
-- account so customer payments (gift cards today; job deposits/escrow
-- later) can route directly to THAT business instead of pooling in the
-- platform's single master Stripe account, which is all that exists today.
--
-- This migration only adds the columns needed to track each business's
-- Connect account id and onboarding state on the existing business_profiles
-- table — it does not change how any existing payment flow (gift cards,
-- subscriptions, wallet unlock) actually routes money yet. That's a
-- deliberate, separate next step once onboarding itself works end to end.
-- Run this in the Supabase SQL editor.

alter table business_profiles
  add column if not exists stripe_connect_account_id text,
  add column if not exists connect_onboarded boolean not null default false,
  add column if not exists connect_payouts_enabled boolean not null default false,
  add column if not exists connect_updated_at timestamptz;

create index if not exists idx_business_profiles_connect_account
  on business_profiles (stripe_connect_account_id);
