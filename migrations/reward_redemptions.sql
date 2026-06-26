-- Closes the loop FASS Rewards was missing: hitting the stamp threshold
-- didn't actually DO anything before this — stamps just kept counting up
-- with no payoff moment for the customer and no way for the business to
-- mark "I gave them the free item" and reset the card for the next round.
-- Run this in the Supabase SQL editor.

-- Running total of how many times this specific card has been redeemed —
-- surfaced to the business as a quick "your most loyal customers" signal.
alter table reward_cards add column if not exists redeemed_count int not null default 0;

-- Audit trail of every redemption, independent of the live stamp count
-- (which resets on redeem). This is what a future "urgency offers" or
-- "referral" feature would read from to know a customer's redemption
-- history, so it's worth keeping even though nothing reads it yet.
create table if not exists reward_redemptions (
  id uuid primary key default gen_random_uuid(),
  card_slug text not null references reward_cards(slug) on delete cascade,
  business_user_id uuid not null,
  stamps_at_redemption int not null,
  redeemed_at timestamptz not null default now()
);

alter table reward_redemptions enable row level security;

drop policy if exists "Owners view their own redemptions" on reward_redemptions;
create policy "Owners view their own redemptions" on reward_redemptions
  for all using (auth.uid() = business_user_id);
