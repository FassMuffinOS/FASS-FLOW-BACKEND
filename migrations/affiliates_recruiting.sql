-- Sub-affiliate recruiting: an affiliate who recruits another affiliate
-- earns a 10% override on everything the recruit earns (one level deep,
-- no further chaining). Reuses the SAME referral link + attribution
-- mechanism that already exists for customer referrals — whoever's code
-- was in profiles.referred_by_code when a user calls POST /affiliates/join
-- becomes their recruiter. No separate "recruiting link" needed.
-- Run this AFTER affiliates.sql (additive only, does not touch existing rows).

alter table public.affiliates
  add column if not exists recruited_by_user_id uuid references auth.users(id) on delete set null;

alter table public.affiliates
  add column if not exists override_rate numeric not null default 0.10;

create index if not exists affiliates_recruited_by_idx on public.affiliates (recruited_by_user_id);

-- Allow a new "override" conversion source — a system-generated row
-- credited to a recruiter whenever their recruit earns a real commission.
-- Never entered by hand in the admin console (that dropdown still only
-- offers the original five), only ever inserted by record_conversion()/
-- admin_log_conversion() in affiliates.py.
alter table public.affiliate_conversions drop constraint if exists affiliate_conversions_source_check;
alter table public.affiliate_conversions add constraint affiliate_conversions_source_check
  check (source in ('subscription', 'wallet_pass', 'masterclass', 'bd_partner', 'other', 'override'));
